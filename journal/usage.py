"""Token usage extraction from upstream response bodies.

Pure: no I/O. Parses Anthropic and OpenAI shapes, JSON and SSE.
Never raises; returns None on any failure or absent usage.
"""

import json


def extract_usage(body, content_type):
    """Return {'input': int, 'output': int} or None.

    Parses Anthropic Messages and OpenAI Chat Completions usage shapes,
    in JSON or SSE bodies.
    """
    if not body:
        return None
    ct = (content_type or "").lower()
    if "event-stream" in ct:
        return _from_sse(body)
    parsed = _safe_json(body)
    if parsed is None:
        return None
    return _from_json_obj(parsed)


def _safe_json(body):
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return None


def _from_json_obj(obj):
    if not isinstance(obj, dict):
        return None
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return None
    if "input_tokens" in usage or "output_tokens" in usage:
        return {
            "input": int(usage.get("input_tokens") or 0),
            "output": int(usage.get("output_tokens") or 0),
        }
    if "prompt_tokens" in usage or "completion_tokens" in usage:
        return {
            "input": int(usage.get("prompt_tokens") or 0),
            "output": int(usage.get("completion_tokens") or 0),
        }
    return None


def _from_sse(body):
    input_tokens = None
    output_tokens = None
    for raw_line in body.splitlines():
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        # Anthropic message_start carries input under message.usage.
        msg = obj.get("message")
        if isinstance(msg, dict):
            mu = msg.get("usage")
            if isinstance(mu, dict):
                if "input_tokens" in mu:
                    input_tokens = int(mu.get("input_tokens") or 0)
                if "output_tokens" in mu:
                    output_tokens = int(mu.get("output_tokens") or 0)
        # Anthropic message_delta carries output under top-level usage.
        # OpenAI streaming final chunk carries usage at top level too.
        u = obj.get("usage")
        if isinstance(u, dict):
            if "input_tokens" in u:
                input_tokens = int(u.get("input_tokens") or 0)
            if "output_tokens" in u:
                output_tokens = int(u.get("output_tokens") or 0)
            if "prompt_tokens" in u:
                input_tokens = int(u.get("prompt_tokens") or 0)
            if "completion_tokens" in u:
                output_tokens = int(u.get("completion_tokens") or 0)
    if input_tokens is None and output_tokens is None:
        return None
    return {"input": input_tokens or 0, "output": output_tokens or 0}
