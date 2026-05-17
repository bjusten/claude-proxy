"""Heuristic request classifier for claude-proxy.

Classifies incoming Claude API requests as "CODING" or "REASONING" based on
content of the last user + last assistant message. Pure functions, no I/O
beyond config load.

Scoring model:
  * Textual signals are scored per KB of inspected text so long code dumps
    don't drown short reasoning prompts.
  * `code_fence_bytes` is a ratio (fence_bytes / text_bytes), naturally [0, 1].
  * Structural signals (`tool_use`, `tool_result`) are saturated at
    TOOL_SATURATION_CAP — presence is informative, repetition is not.
  * The last user message is weighted USER_WEIGHT× the last assistant message
    because the user's current turn carries intent; the assistant turn carries
    past context.
  * A KB_FLOOR prevents very short messages from producing absurd densities.
"""

import json
import logging
import os
import re
import time
import urllib.request

logger = logging.getLogger(__name__)

USER_WEIGHT = 2.0
KB_FLOOR = 0.1
TOOL_SATURATION_CAP = 3

DEFAULT_WEIGHTS = {
    "code_fence":        3.0,
    "code_fence_bytes":  5.0,
    "tool_use":          2.0,
    "tool_result":       2.0,
    "code_token":        0.3,
    "failure_marker":    2.0,
    "file_path":         1.5,
    "reasoning_verb":    2.0,
    "question_mark":     0.5,
}

DEFAULT_LLM_CLASS_MAP = {
    "CODING":    ["coding", "debugging", "refactoring", "testing", "devops", "data"],
    "REASONING": ["planning", "reasoning", "research", "analysis", "math",
                  "writing", "summarization", "translation", "explanation",
                  "brainstorming", "conversation", "creative"],
}

DEFAULT_STRIP_TAGS = [
    "system-reminder",
    "command-name",
    "command-message",
    "command-args",
    "local-command-caveat",
    "local-command-stdout",
]

DEFAULT_CONFIG = {
    "enabled": False,
    "default": "CODING",
    "min_confidence": 1.0,
    "max_scan_bytes_per_message": 65536,
    "budget_warn_ms": 1000,
    "method": "internal",
    "llm_url": "http://127.0.0.1:8090/v1/chat/completions",
    "llm_model": "qwen2.5-3b",
    "llm_api_key": "",
    "llm_timeout_ms": 5000,
    "llm_max_tokens": 1024,       # max tokens the classification LLM may return
    "llm_max_input_chars": 2048,  # max chars sent to the classification LLM
    "llm_class_map": DEFAULT_LLM_CLASS_MAP,
    "weights": DEFAULT_WEIGHTS,
    "strip_tags": DEFAULT_STRIP_TAGS,
}

VALID_CLASSES = ("CODING", "REASONING")


def load_config(path):
    """Load classification.json, creating a default if missing.

    Returns a dict with all fields present (missing fields filled from
    DEFAULT_CONFIG). Raises ValueError on invalid `default` value.
    """
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        return {
            **DEFAULT_CONFIG,
            "weights": dict(DEFAULT_WEIGHTS),
            "llm_class_map": json.loads(json.dumps(DEFAULT_LLM_CLASS_MAP)),
        }

    with open(path, "r") as f:
        user_cfg = json.load(f)

    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: v for k, v in user_cfg.items() if k != "weights"})
    # Merge weights so the user file can override individual signals only.
    merged_weights = dict(DEFAULT_WEIGHTS)
    merged_weights.update(user_cfg.get("weights", {}))
    cfg["weights"] = merged_weights

    if cfg["default"] not in VALID_CLASSES:
        raise ValueError(
            f"classification.json: 'default' must be one of {VALID_CLASSES}, got {cfg['default']!r}"
        )

    if cfg.get("method") == "llm":
        if not cfg.get("llm_url"):
            raise ValueError(
                "classification.json: 'llm_url' is required when method == 'llm'"
            )
        cls_map = cfg.get("llm_class_map")
        if not cls_map:
            raise ValueError(
                "classification.json: 'llm_class_map' is required when method == 'llm'"
            )
        try:
            words, inverted = _derive_llm_lookup(cls_map)
        except ValueError as e:
            raise ValueError(f"classification.json: {e}")
        if not words:
            raise ValueError(
                "classification.json: 'llm_class_map' must contain at least one class bucket with at least one word"
            )
        cfg["_llm_words"] = words
        cfg["_llm_inverted"] = inverted
    return cfg


def _strip_context_tags(text, tags):
    """Remove XML-style tags matching *tags* and their content from *text*.

    For each tag name in *tags*, removes the opening tag, closing tag, and
    everything in between (non-greedy).  Then collapses multiple blank lines
    and strips leading/trailing whitespace.

    Example::

        <system-reminder>hello</system-reminder>Hello, world!
        → "Hello, world!"
    """
    if not text:
        return text

    for tag in tags:
        # Match <tag>...</tag> (non-greedy) and self-closing <tag .../>
        text = re.sub(rf"<{tag}(?:\s[^>]*)?>.*?</{tag}>\s*", "", text, flags=re.DOTALL)
        text = re.sub(rf"<{tag}(?:\s[^>]*)*/>\s*", "", text, flags=re.DOTALL)

    # Collapse 2+ consecutive newlines down to a single blank line
    text = re.sub(r"\n{2,}", "\n\n", text)
    return text.strip()


def _derive_llm_lookup(class_map):
    """Flatten llm_class_map into (word_list, word->key) deriving both from one
    source so the prompt's allowed words and the response lookup can't drift.

    Map-key order then word order is preserved → deterministic prompt string.
    Raises ValueError on an unknown destination key or a word reused across
    two keys (which would make the inverted map ambiguous).
    """
    words = []
    inverted = {}
    for key, bucket in class_map.items():
        if key not in VALID_CLASSES:
            raise ValueError(
                f"classification.json: llm_class_map key {key!r} must be one of {VALID_CLASSES}"
            )
        for word in bucket:
            if word in inverted:
                raise ValueError(
                    f"classification.json: llm_class_map word {word!r} appears under two keys"
                )
            inverted[word] = key
            words.append(word)
    return words, inverted


def _flatten_content(content):
    """Flatten Anthropic message content (string or list of blocks) to (text, counts).

    counts is a dict with structural counters: tool_use, tool_result.
    """
    counts = {"tool_use": 0, "tool_result": 0}
    if isinstance(content, str):
        return content, counts
    if not isinstance(content, list):
        return "", counts

    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "tool_use":
            counts["tool_use"] += 1
            tool_input = block.get("input", {})
            if isinstance(tool_input, dict):
                parts.append(json.dumps(tool_input))
            else:
                parts.append(str(tool_input))
        elif btype == "tool_result":
            counts["tool_result"] += 1
            inner = block.get("content", "")
            if isinstance(inner, str):
                parts.append(inner)
            elif isinstance(inner, list):
                for sub in inner:
                    if isinstance(sub, dict) and sub.get("type") == "text":
                        parts.append(sub.get("text", ""))
    return "\n".join(parts), counts


def _find_last(messages, role):
    """Return the last message with the given role, or None."""
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == role:
            return msg
    return None


def extract_inspection_text(body, max_bytes):
    """Extract scan text + structural counters from last user + last assistant.

    Returns (text, counts_dict). System field is intentionally skipped.
    Each per-message text is truncated to the last `max_bytes`.
    """
    counts = {"tool_use": 0, "tool_result": 0}
    messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list) or not messages:
        return "", counts

    chunks = []
    for role in ("user", "assistant"):
        msg = _find_last(messages, role)
        if msg is None:
            continue
        text, msg_counts = _flatten_content(msg.get("content", ""))
        if len(text) > max_bytes:
            text = text[-max_bytes:]
        chunks.append(text)
        counts["tool_use"] += msg_counts["tool_use"]
        counts["tool_result"] += msg_counts["tool_result"]
    return "\n".join(chunks), counts


_FENCE_RE = re.compile(r"```([^`]*?)```", re.DOTALL)
_CODE_TOKEN_RE = re.compile(
    r"[{}();]|=>|->|::|\b(?:def|class|import|return|function|const|let|var|async|await|throw|catch)\b"
)
_FAILURE_RE = re.compile(
    r"\bTraceback\b|\bError:|\bException\b|\bFAIL\b|\bpanic:|\bSyntaxError\b|\bat\s+\S+\s+\([^)]*:\d+\)"
)
_FILE_PATH_RE = re.compile(
    r"\b[\w./-]+\.(?:py|ts|tsx|js|jsx|rs|go|java|rb|c|cpp|h|hpp|json|yaml|yml|toml|md|sh|sql)\b"
)
_REASONING_VERB_RE = re.compile(
    r"\b(?:"
    r"explain|explanation|why|"
    r"how\s+(?:does|do|would|should|could|works?|come)|"
    r"what\s+(?:is|are|do\s+you\s+think|would|should|happens?)|"
    r"compare|analyze|analysis|summarize|summary|"
    r"design|approach|consider|alternative|"
    r"thoughts?|opinion|opinions|"
    r"recommend|recommendation|suggest|suggestion|"
    r"should\s+i|would\s+you|"
    r"tradeoffs?|trade-offs?|"
    r"pros\s+and\s+cons"
    r")\b",
    re.IGNORECASE,
)


def count_signals(text, structural_counts):
    """Count each signal in `text`. structural_counts carries tool_use/tool_result.

    Returns raw, unnormalized counts (plus `text_bytes` for downstream scaling).
    Structural counts are saturated at TOOL_SATURATION_CAP — many tool calls in
    a single turn shouldn't outweigh everything else. Normalization to per-KB
    density happens in `_score_signals`, not here.
    """
    if not text:
        return {
            "code_fence": 0,
            "code_fence_bytes": 0,
            "tool_use": min(structural_counts.get("tool_use", 0), TOOL_SATURATION_CAP),
            "tool_result": min(structural_counts.get("tool_result", 0), TOOL_SATURATION_CAP),
            "code_token": 0,
            "failure_marker": 0,
            "file_path": 0,
            "reasoning_verb": 0,
            "question_mark": 0,
            "text_bytes": 0,
        }

    fences = _FENCE_RE.findall(text)
    fence_bytes = sum(len(f) for f in fences)

    return {
        "code_fence": len(fences),
        "code_fence_bytes": fence_bytes,
        "tool_use": min(structural_counts.get("tool_use", 0), TOOL_SATURATION_CAP),
        "tool_result": min(structural_counts.get("tool_result", 0), TOOL_SATURATION_CAP),
        "code_token": len(_CODE_TOKEN_RE.findall(text)),
        "failure_marker": len(_FAILURE_RE.findall(text)),
        "file_path": len(_FILE_PATH_RE.findall(text)),
        "reasoning_verb": len(_REASONING_VERB_RE.findall(text)),
        "question_mark": text.count("?"),
        "text_bytes": len(text),
    }


_DENSITY_CODE_SIGNALS = ("code_fence", "code_token", "failure_marker", "file_path")
_DENSITY_REASONING_SIGNALS = ("reasoning_verb", "question_mark")
_STRUCTURAL_CODE_SIGNALS = ("tool_use", "tool_result")


def _score_signals(signals, weights):
    """Convert raw signals to weighted (code_score, reasoning_score).

    Textual signals are normalized to occurrences per KB, with a KB_FLOOR
    so tiny messages can't generate absurd densities. `code_fence_bytes`
    is treated as a ratio (fence_bytes / text_bytes, capped at 1.0).
    Structural signals are added as-is (already saturated upstream).
    """
    text_bytes = signals.get("text_bytes", 0)
    kb = max(text_bytes / 1024.0, KB_FLOOR)

    code = 0.0
    for k in _DENSITY_CODE_SIGNALS:
        code += (signals[k] / kb) * weights.get(k, 0.0)
    fence_ratio = signals["code_fence_bytes"] / max(text_bytes, 1)
    code += min(fence_ratio, 1.0) * weights.get("code_fence_bytes", 0.0)
    for k in _STRUCTURAL_CODE_SIGNALS:
        code += signals[k] * weights.get(k, 0.0)

    reasoning = 0.0
    for k in _DENSITY_REASONING_SIGNALS:
        reasoning += (signals[k] / kb) * weights.get(k, 0.0)

    return code, reasoning


def _score_one(messages, role, max_bytes, weights, strip_tags=None):
    """Locate the last message of `role`, score it. Returns (code, reasoning).

    *strip_tags* is an optional list of tag names whose XML-style blocks
    are removed from the text before signal counting.
    """
    msg = _find_last(messages, role)
    if msg is None:
        return 0.0, 0.0
    text, struct = _flatten_content(msg.get("content", ""))
    if len(text) > max_bytes:
        text = text[-max_bytes:]
    if strip_tags:
        text = _strip_context_tags(text, strip_tags)
    signals = count_signals(text, struct)
    return _score_signals(signals, weights)


def classify(body, cfg):
    """Classify a request body. Returns (class_string, scores_dict).

    scores_dict has keys: code, reasoning, scan_ms.
    The caller is expected to only invoke this when cfg["enabled"] is True.

    The last user message is weighted USER_WEIGHT× the last assistant message:
    user intent is the strongest signal of what the request is actually for;
    the assistant turn is only context.
    """
    t0 = time.monotonic()
    weights = cfg["weights"]
    max_bytes = cfg["max_scan_bytes_per_message"]
    strip_tags = cfg.get("strip_tags", DEFAULT_STRIP_TAGS)
    messages = body.get("messages") if isinstance(body, dict) else None

    user_code = user_reason = asst_code = asst_reason = 0.0
    if isinstance(messages, list) and messages:
        user_code, user_reason = _score_one(messages, "user", max_bytes, weights, strip_tags)
        asst_code, asst_reason = _score_one(messages, "assistant", max_bytes, weights, strip_tags)

    code_score = USER_WEIGHT * user_code + asst_code
    reasoning_score = USER_WEIGHT * user_reason + asst_reason
    scan_ms = (time.monotonic() - t0) * 1000.0

    scores = {"code": code_score, "reasoning": reasoning_score, "scan_ms": scan_ms}

    if max(code_score, reasoning_score) < cfg["min_confidence"]:
        return cfg["default"], scores
    return ("CODING" if code_score >= reasoning_score else "REASONING"), scores


# --- LLM-backed classification path ----------------------------------------


def _text_blocks(content):
    """Return only the textual content of a message (string or block list).

    Non-text blocks (tool_use, image, etc.) are excluded — tool details
    are extracted separately via _tool_calls() for the classification prompt.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)


def _tool_calls(content):
    """Tool call details (name + input) of any tool_use blocks in a message, order preserved.

    Returns a list of strings, each formatted as:
        tool <name>(<json_input>)
    """
    lines = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name", "?")
                inp = json.dumps(block.get("input", {}))
                lines.append(f"tool {name}({inp})")
    return lines


def _build_llm_input(body, cfg):
    """Minimal inspection string: last user text (tail-truncated) plus a single
    `tools: a, b` line of deduped tool names from the last user + assistant turn.
    """
    messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list):
        messages = []
    last_user = _find_last(messages, "user")
    last_asst = _find_last(messages, "assistant")

    text = _text_blocks(last_user.get("content", "")) if last_user else ""
    max_bytes = cfg["max_scan_bytes_per_message"]
    if len(text) > max_bytes:
        text = text[-max_bytes:]
    strip_tags = cfg.get("strip_tags", DEFAULT_STRIP_TAGS)
    text = _strip_context_tags(text, strip_tags)

    calls = []
    for msg in (last_user, last_asst):
        if not msg:
            continue
        calls.extend(_tool_calls(msg.get("content", "")))

    if calls:
        calls_line = "\n".join(calls)
        text = text + "\n" + calls_line if text else calls_line
    return text


def _call_llm(text, cfg):
    """POST the inspection string to an OpenAI-compatible /chat/completions
    endpoint and return the raw response body. Stdlib urllib only.

    Input is truncated to a hard limit to mitigate adversarial prompt
    injection attacks against the classifier LLM.
    """
    # Hard cap on user-supplied text sent to the LLM (prevents prompt
    # injection / context-window exhaustion from untrusted input).
    max_input = cfg.get("llm_max_input_chars", 2048)
    if len(text) > max_input:
        text = text[:max_input]
    words = cfg["_llm_words"]
    allowed = ", ".join(words)
    system = (
        "You are a classification model. Given a user request, classify it into exactly ONE category.\n\n"
        "Allowed categories: " + allowed + ".\n\n"
        "Rules:\n"
        "- Respond with exactly ONE category word from the allowed list.\n"
        "- No punctuation, no explanation, no quotes.\n"
        "- If the input is too ambiguous, garbled, or you genuinely cannot determine a category, respond with:\n"
        "  ERROR: <brief reason why you cannot classify>\n"
    )
    words = cfg["_llm_words"]
    allowed = ", ".join(words)
    system = (
        "You are a classification model. Given a user request, classify it into exactly ONE category.\n\n"
        "Allowed categories: " + allowed + ".\n\n"
        "Rules:\n"
        "- Respond with exactly ONE category word from the allowed list.\n"
        "- No punctuation, no explanation, no quotes.\n"
        "- If the input is too ambiguous, garbled, or you genuinely cannot determine a category, respond with:\n"
        "  ERROR: <brief reason why you cannot classify>\n"
    )
    payload = {
        "model": cfg["llm_model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        "temperature": 0,
        "max_tokens": cfg.get("llm_max_tokens", 1024),
        "stream": False,
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    api_key = cfg.get("llm_api_key", "")
    if api_key:
        headers["Authorization"] = "Bearer " + api_key

    req = urllib.request.Request(
        cfg["llm_url"], data=data, headers=headers, method="POST"
    )
    timeout = cfg["llm_timeout_ms"] / 1000.0
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", 200)
        if status != 200:
            raise RuntimeError(f"LLM classifier returned status {status}")
        raw = resp.read().decode("utf-8")
        return raw, payload


_LLM_WORD_RE = re.compile(r"[a-z]+")


_LLM_ERROR_RE = re.compile(r"\bERROR\b", re.IGNORECASE)


def _parse_llm_response(raw, inverted_map):
    """Extract choices[0].message.content and map the first recognized
    lowercase token to a destination key. Anything unparseable or unmapped
    raises (→ heuristic fallback).

    Returns ``(key, raw_word)``: the mapped destination key plus the exact
    token the LLM emitted (e.g. ``("REASONING", "research")``) so callers
    can surface the AI's own word alongside the routed class. ERROR content
    returns ``("ERROR", None)``.
    """
    try:
        obj = json.loads(raw)
        content = obj["choices"][0]["message"].get("content", "")
        if not content:
            content = obj["choices"][0]["message"].get("reasoning_content", "")
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise ValueError(f"LLM classifier response unparseable: {e}")
    if not isinstance(content, str):
        raise ValueError("LLM classifier response missing text content")
    if _LLM_ERROR_RE.search(content):
        return "ERROR", None
    for token in _LLM_WORD_RE.findall(content.lower()):
        if token in inverted_map:
            return inverted_map[token], token
    raise ValueError(f"LLM classifier returned no mappable word: {content!r}")


def classify_request(body, cfg):
    """Dispatch entry point. Same (key, scores) shape as `classify()`.

    `cfg["method"] != "llm"` → heuristic. `"llm"` → LLM path, falling back to
    the heuristic on any failure. `scores["method"]` is one of
    "internal" | "llm" | "llm_fallback"; "code"/"reasoning"/"scan_ms" are
    always present so downstream log/journal code is untouched.
    """
    if cfg.get("method") != "llm":
        key, scores = classify(body, cfg)
        scores["method"] = "internal"
        return key, scores

    t0 = time.monotonic()
    text = _build_llm_input(body, cfg)
    exchange = {
        "url": cfg.get("llm_url"),
        "model": cfg.get("llm_model"),
        "input": text,
        "status": None,
        "response": None,
        "classified": None,
        "raw_class": None,
        "fallback": False,
        "error": None,
        "duration_ms": 0.0,
    }
    try:
        call_start = time.monotonic()
        raw, payload = _call_llm(text, cfg)
        call_duration_ms = (time.monotonic() - call_start) * 1000.0
        key, raw_class = _parse_llm_response(raw, cfg["_llm_inverted"])
        scan_ms = (time.monotonic() - t0) * 1000.0
        exchange.update(status=200, response=raw, classified=key,
                        raw_class=raw_class, payload=payload,
                        duration_ms=scan_ms, call_duration_ms=call_duration_ms)
        if key == "ERROR":
            return key, {"code": 0.0, "reasoning": 0.0, "scan_ms": scan_ms,
                         "method": "llm", "llm_exchange": exchange}
        return key, {"code": 0.0, "reasoning": 0.0, "scan_ms": scan_ms,
                     "method": "llm", "llm_exchange": exchange}
    except Exception as e:
        scan_ms = (time.monotonic() - t0) * 1000.0
        logger.warning("LLM classification failed, falling back to heuristic: %s", e)
        key, scores = classify(body, cfg)
        scores["method"] = "llm_fallback"
        exchange.update(fallback=True, error=str(e), classified=key, duration_ms=scan_ms)
        scores["llm_exchange"] = exchange
        return key, scores
