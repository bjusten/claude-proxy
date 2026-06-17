# Configuration

`claude-proxy` reads four JSON config files. Each is auto-created with defaults on first run if missing, and each path is overridable on the command line:

| File | CLI flag | Purpose |
| --- | --- | --- |
| `models.json` | `--models-config` | Routing: which incoming model/class goes to which backend(s) |
| `proxy.json` | `--proxy-config` | Proxy-wide settings: upstream timeout, global URL tracking, wait timeout, session logging |
| `classification.json` | `--classification-config` | Heuristic `CODING`/`REASONING` classifier |
| `journal.json` | `--journal-config` | In-memory activity journal, telemetry pollers, and web UI |

Relative paths are resolved next to `proxy.py`; absolute paths are used as-is. **Config is read once at startup — restart the proxy after editing any file.**

---

## `models.json` — routing

This file maps a **routing key** to one or more backends. It contains routing entries only — proxy-wide scalars live in `proxy.json` (below). The routing key is normally the `model` field from the incoming request body; with classification enabled (see below) it is instead `CODING` or `REASONING`.

Each entry has a `urls` list. Every list item is `{ "url": <backend base URL>, "new_model_name": <model the backend expects> }`. When the request is forwarded, its `model` field is rewritten to the chosen entry's `new_model_name`.

### The three setup modes

The proxy supports three ways to route, all expressed in this one file.

#### Mode 1 — Single destination

An incoming model maps to **exactly one** backend. Simplest case: rename a model and forward it to one host.

```json
{
  "claude-opus-4-7": {
    "urls": [
      { "url": "http://127.0.0.1:8091", "new_model_name": "qwen3:70b" }
    ]
  },
  "default": {
    "urls": [
      { "url": "http://127.0.0.1:8092", "new_model_name": "qwen3:8b" }
    ]
  }
}
```

A request for `qwen3.6` is sent to `http://127.0.0.1:8092/v1/...` with `model` rewritten to `qwen3:8b` (the `default` entry). With a single-element `urls` list there is no load balancing and no connection accounting.

#### Mode 2 — Backend pool (priority + least-active-connections)

An incoming model maps to **several** backends. For each request the proxy picks the backend with the **fewest in-flight connections**; ties are broken by **list position** (earlier entries win). This both load-balances a model pool and lets you express priority — put the preferred/fastest backend first so it's chosen whenever the pool is otherwise idle.

```json
{
  "qwen3.6": {
    "urls": [
      { "url": "http://gpu-a:11434", "new_model_name": "qwen3:70b" },
      { "url": "http://gpu-b:11434", "new_model_name": "qwen3:70b" },
      { "url": "http://gpu-c:11434", "new_model_name": "qwen3:8b" }
    ]
  }
}
```

Connections are counted per routing key. The active count rises on acquire and falls on release; you can watch it live in `claude-proxy-sessions.log` (`active=` / `idx=` fields).

#### Mode 3 — Classified routing (`CODING` / `REASONING`)

When classification is enabled (`classification.json`, below), the routing key is no longer the requested model — it's the classifier's verdict. Key your routes by `CODING` and `REASONING` so a coding-tuned model and a reasoning-tuned model can be targeted without the client knowing which to ask for. Each class is itself a single destination (Mode 1) or a pool (Mode 2).

```json
{
  "CODING": {
    "urls": [
      { "url": "http://coding-host:8083", "new_model_name": "coding-model" }
    ]
  },
  "REASONING": {
    "urls": [
      { "url": "http://reason-a:8083", "new_model_name": "reasoning-model" },
      { "url": "http://reason-b:8083", "new_model_name": "reasoning-model" }
    ]
  },
  "default": {
    "urls": [
      { "url": "http://127.0.0.1:11434", "new_model_name": "qwen3:latest" }
    ]
  }
}
```

### The `default` catch-all

`default` is a reserved key. Any routing key with no explicit entry — an unmapped model, or a classifier verdict you didn't key a route for — falls through to `default`. Always define it so unexpected models still resolve somewhere. If a key is unmatched **and** there is no `default`, the proxy falls back to a hard-coded `http://127.0.0.1:11434` with model `qwen3.6:27b`.

### Field reference

| Field | Required | Meaning |
| --- | --- | --- |
| *(top-level key)* | yes | Routing key: an incoming model name, `CODING`/`REASONING` (classified mode), or `default` |
| `urls` | yes (modern) | List of backend entries; 1 entry = single destination, >1 = pool |
| `urls[].url` | yes | Backend **base URL** (scheme + host + port, no path). The request path (`/v1/...`) is appended. |
| `urls[].new_model_name` | no | Model name written into the forwarded body. Defaults to the original model if omitted. |
| `url` | alternate | Single-backend shorthand. If `urls` is absent, one `url` (+ optional `new_model_name`) is used directly with no balancing. Ignored when `urls` is present. |

Notes:
- Only `/v1/*` paths are proxied; any other path returns `404`.
- The `Host` header is rewritten to the upstream target and `Content-Length` recomputed after the model rewrite.
- Upstream responses are streamed back to the client in 64 KiB chunks as they arrive (no full-body buffering before the client sees data).
- The auto-created default `models.json` contains placeholder `model-a`/`model-b` entries pointing at `1.2.3.4` — replace them.

---

## `proxy.json` — proxy settings

Proxy-wide scalar options. The auto-created default contains every option pre-written at its default:

```json
{
  "upstream_timeout_seconds": 300,
  "global_url_tracking": false,
  "wait_timeout_seconds": 300,
  "enable_session_logging": false
}
```

### `upstream_timeout_seconds`

Default `300`. Sets the socket timeout for the forwarded upstream request. It applies **per blocking socket read**, so a streaming model that keeps sending data resets it on every chunk; it only trips after that many seconds of no upstream activity. Set to `null` to disable the timeout entirely (block until the model responds).

### `global_url_tracking` — deduplicate active connections across routes

When enabled (`"global_url_tracking": true`), active connections are tracked **globally by host:port** across all routing keys instead of per-key. If two routes (in `models.json`) share the same upstream URL, only one route can hold an active connection to it at a time — the other picks the next available entry in its pool. This is essential when routing to local models that reject concurrent requests.

```json
{
  "global_url_tracking": true
}
```

With a `models.json` where `default` and `claude-opus-4` both pool `http://127.0.0.1:11434` and `http://remote:11434`: the first request to `claude-opus-4` acquires `http://127.0.0.1:11434`; the next request to `default` sees that URL is already in use and picks `http://remote:11434` instead. When the first request completes and releases, `http://127.0.0.1:11434` becomes available again.

### `wait_timeout_seconds` — wait for a free URL when all are busy

When `global_url_tracking` is enabled and every URL in a route's pool is currently in use, the proxy can **wait** for a slot to free up instead of immediately failing the request. `wait_timeout_seconds` is in seconds. Default: `300` (5 minutes).

```json
{
  "global_url_tracking": true,
  "wait_timeout_seconds": 300
}
```

- Set to `0` to disable waiting — all URLs busy → pick the least-active anyway, immediately, without blocking.
- Waiting connections are woken the instant a slot is released (no polling delay).
- **Queued connections have strict FIFO priority over newer arrivals.** A freed slot is reserved for the longest-waiting connection; a connection that arrives later cannot jump ahead of one already queued, even if it observes the free slot first.
- If the timeout expires, the proxy picks the entry with the fewest active connections (tie-break by index) and proceeds — it will not block forever.

### `enable_session_logging` — log session ACQUIRE/RELEASE

Default `false`. When enabled (`"enable_session_logging": true`), the proxy writes an `ACQUIRED` line to the session log when a request acquires an upstream connection and a `RELEASED` line when it completes, each carrying the active session count, routing key, chosen URL, and rewritten model name (the `ACQUIRED` line also includes classifier scores when classification is enabled). Disabled by default to keep the log quiet; enable it to trace concurrency and routing behaviour.

```json
{
  "enable_session_logging": true
}
```

---

## `classification.json` — request classifier

When `enabled` is `true`, every request is classified and the routing key becomes `CODING` or `REASONING` instead of the requested model. When `false` (default), this file is inert and routing is purely model-based.

Two classification methods are available, selected by `method`:

- `"internal"` (default) — the built-in heuristic scorer. No LLM call. Classifies strictly into `CODING` or `REASONING` based on textual and structural signals in the request.
- `"llm"` — ask an OpenAI-compatible LLM to classify the request using a **configurable class map** (`llm_class_map`). The heuristic remains the automatic fallback whenever the LLM path fails (timeout, network error, non-200, or an unrecognized answer). See [LLM-backed classification](#llm-backed-classification-method-llm) below.

```json
{
  "enabled": false,
  "default": "CODING",
  "min_confidence": 1.0,
  "max_scan_bytes_per_message": 65536,
  "budget_warn_ms": 1000,
  "method": "internal",
  "llm_url": "http://127.0.0.1:8090/v1/chat/completions",
  "llm_model": "qwen2.5-3b",
  "llm_api_key": "",
  "llm_timeout_ms": 5000,
  "llm_max_tokens": 1024,
  "llm_max_input_chars": 2048,
  "llm_class_map": {
    "CODING":    ["coding", "debugging", "refactoring", "testing", "devops", "data"],
    "REASONING": ["planning", "reasoning", "research", "analysis", "math",
                  "writing", "summarization", "translation", "explanation",
                  "brainstorming", "conversation", "creative"]
  },
  "weights": {
    "code_fence":        3.0,
    "code_fence_bytes":  5.0,
    "tool_use":          2.0,
    "tool_result":       2.0,
    "code_token":        0.3,
    "failure_marker":    2.0,
    "file_path":         1.5,
    "reasoning_verb":    2.0,
    "question_mark":     0.5
  }
}
```

| Field | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Master switch. `false` → route by requested model (file ignored). |
| `default` | `"CODING"` | Class used when confidence is below `min_confidence`. Must be `"CODING"` or `"REASONING"` (invalid value aborts startup). |
| `min_confidence` | `1.0` | If `max(code_score, reasoning_score)` is below this, fall back to `default`. |
| `max_scan_bytes_per_message` | `65536` | Per-message scan cap; only the **last** this-many bytes of each inspected message are scored. |
| `budget_warn_ms` | `1000` | If a classification takes longer than this, a warning is logged (routing is unaffected). Covers the LLM path too. |
| `method` | `"internal"` | `"internal"` — heuristic only, classifies to `CODING` or `REASONING`. `"llm"` — ask an LLM using `llm_class_map`; falls back to heuristic on failure. |
| `llm_url` | `http://127.0.0.1:8090/v1/chat/completions` | OpenAI-compatible chat-completions endpoint. Required when `method` is `"llm"`. |
| `llm_model` | `"qwen2.5-3b"` | Model name sent in the classification request. |
| `llm_api_key` | `""` | Optional. When non-empty, sent as `Authorization: Bearer <key>`. Empty → no auth header (keyless local servers). |
| `llm_timeout_ms` | `5000` | Hard timeout for the classification call. On timeout → heuristic fallback. |
| `llm_class_map` | see above | Destination key (`CODING`/`REASONING`) → list of allowed one-word LLM answers. Validated at startup (see below). |
| `weights` | see above | Per-signal weights (heuristic path). You may override individual keys; unspecified keys keep their defaults. |
| `strip_tags` | `["system-reminder", "command-name", "command-message", "command-args", "local-command-caveat", "local-command-stdout"]` | XML-style tag names whose content is stripped from the scanned text before signal counting. Removes context boilerplate (e.g. `<system-reminder>`, `<command-name>`) that would otherwise pollute the heuristic. Applies to both internal and LLM methods. |

### How scoring works

- Only the **last user message** and **last assistant message** are inspected; the `system` field is intentionally skipped.
- Textual signals are normalized to **per-KB density** (with a small floor so a tiny message can't manufacture a huge density). `code_fence_bytes` is a ratio (fenced bytes ÷ total bytes, capped at 1.0).
- Structural signals (`tool_use`, `tool_result`) **saturate at 3** — more tool calls than that add nothing.
- The **last user message is weighted 2×** the last assistant message: current intent matters more than past context.
- Highest score wins; below `min_confidence` → `default`.

| Signal | Class | Matches |
| --- | --- | --- |
| `code_fence` | code | Count of ```` ``` ```` blocks |
| `code_fence_bytes` | code | Ratio of fenced-code bytes to total text |
| `tool_use` | code | `tool_use` content blocks (cap 3) |
| `tool_result` | code | `tool_result` content blocks (cap 3) |
| `code_token` | code | `{ } ; => -> ::` and `def class import return function const let var async await throw catch` |
| `failure_marker` | code | `Traceback`, `Error:`, `Exception`, `FAIL`, `panic:`, `SyntaxError`, `at file.js:10` |
| `file_path` | code | Paths ending in a code extension (`.py`, `.ts`, `.rs`, `.go`, `.json`, …) |
| `reasoning_verb` | reasoning | `explain`, `why`, `how does/works`, `what is/are`, `compare`, `analyze`, `design`, `approach`, `consider`, `alternative`, `recommend`, `should i`, `tradeoffs`, `pros and cons`, … |
| `question_mark` | reasoning | `?` occurrences |

When classification is active, stdout and `claude-proxy-sessions.log` include the verdict and `code=`/`reasoning=` scores per request. Score magnitudes in the tens-to-hundreds are normal for a populated request.

### LLM-backed classification (`method: "llm"`)

With `method` set to `"llm"`, the proxy asks an OpenAI-compatible model to name the request in a single word, then maps that word to a routing key. It optimizes for speed and a minimal payload: `temperature: 0`, no streaming, stdlib `urllib` only.

**Configurable LLM parameters** (all default to conservative values):

| Key | Default | Description |
|-----|---------|-------------|
| `llm_max_tokens` | `1024` | Max tokens the classification LLM may return |
| `llm_max_input_chars` | `2048` | Max chars sent to the classification LLM (prevents context-window exhaustion from untrusted input) |
| `llm_timeout_ms` | `5000` | Timeout for the LLM classification call |

- **Context stripping:** before sending text to the LLM, XML-style tags listed in `strip_tags` (e.g. `<system-reminder>`, `<command-name>`) are removed so classifier boilerplate doesn't pollute the classification decision. This also applies to the internal heuristic path.
- **What gets sent:** the last user message's text (tail-truncated to `max_scan_bytes_per_message`) plus a single `tools: name1, name2` line listing deduped tool names from the last user/assistant turns. No tool inputs, no `tool_result` bodies.
- **Allowed words & lookup are derived from `llm_class_map` only** — there is no separate word-list setting, so the prompt and the response lookup can never drift. Word order in the prompt = map-key order then word order.
- **Internal vs LLM scope:** the internal heuristic classifies strictly into `CODING` or `REASONING`. The LLM method can accept any words you configure in `llm_class_map`, but every map key must still be `CODING` or `REASONING` — the proxy routes on these two internal classes regardless of method. Use the class map to teach the LLM the vocabulary of your domain so it can distinguish nuances (e.g. "debugging" vs "research") that the heuristic might miss.
- **Startup validation (only when `method == "llm"`):** `llm_url` must be set; every `llm_class_map` key must be `CODING` or `REASONING`; no word may appear under two keys. Any violation aborts startup with a `ValueError`.
- **Fallback:** any failure — timeout, network error, non-200, unparseable response, or an answer not in `llm_class_map` — falls back to the heuristic. The fallback is logged at WARNING and the request is still classified.
- **`scores.method`** is `"internal"`, `"llm"`, or `"llm_fallback"` so you can tell which path ran. `scan_ms` measures whichever path ran, so `budget_warn_ms` covers LLM latency too.

Every LLM classification call (success or fallback) is also recorded in the activity journal (when journaling is enabled) under the synthetic session id **`classification requests`**, so all classifier traffic is reviewable as a single grouped row in the UI, separate from user requests. The entry captures the endpoint, the inspection payload sent, the model's answer, the resulting class, and timing.

---

## `journal.json` — activity journal, telemetry & web UI

Optional and **disabled by default**. When enabled, the proxy keeps an in-memory activity journal (proxy connections, GPU/system samples, token usage), exposes a read-only `/__journal/*` API, and can serve a password-gated web UI at `/__ui/`. **Nothing is persisted to disk** — the journal is dropped when the proxy exits.

```json
{
  "enabled": false,
  "max_bytes": 1073741824,
  "max_age_seconds": 7200,
  "max_body_bytes": 10240,
  "redact_fields": ["system", "api_key", "Authorization", "api_secret", "private_key"],
  "enable_gpu": true,
  "enable_system": true,
  "gpu_poll_interval_seconds": 5,
  "system_poll_interval_seconds": 10,
  "disk_mounts": ["/"],
  "ui": {
    "enabled": false,
    "admin_password": "",
    "session_ttl_seconds": 43200,
    "theme": "system",
    "show_gpu": true,
    "show_system": true,
    "state_colors": {
      "INIT": "#9e9e9e",
      "CLASSIFYING": "#9c27b0",
      "QUEUED": "#ff9800",
      "ROUTING_REQUEST": "#2196f3",
      "ROUTING_RESPONSE": "#3f51b5",
      "SUCCESS": "#4caf50",
      "FAILURE": "#f44336"
    }
  }
}
```

### Top-level fields

| Field | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Master switch. `false` → no journal allocated; `/__journal/*` falls through to upstream routing like any other path. |
| `max_bytes` | `1073741824` (1 GiB) | Soft cap on journal size; oldest unpinned entries are evicted past this. |
| `max_age_seconds` | `7200` (2 h) | Entries older than this are evicted by a background tick. |
| `enable_gpu` | `true` | When `false`, the GPU poller is never started: no `nvidia-smi` sampling, no `gpu_change` events, `/__journal/gpu` stays empty, and the UI hides the GPU toggle. |
| `enable_system` | `true` | When `false`, the system poller is never started: no CPU/mem/disk sampling, no `system_change` events, `/__journal/system` stays empty, and the UI hides the system toggle. |
| `max_body_bytes` | `10240` | Max request/response body bytes kept per journal entry; larger bodies are truncated (see [Body redaction](#body-redaction)). |
| `redact_fields` | `["system", "api_key", "Authorization", "api_secret", "private_key"]` | Body field names replaced with `"[redacted]"` before an entry is stored (see [Body redaction](#body-redaction)). Setting this key **replaces** the default list verbatim. |
| `gpu_poll_interval_seconds` | `5` | Interval for the `nvidia-smi` GPU poller (ignored when `enable_gpu` is false). |
| `system_poll_interval_seconds` | `10` | Interval for the system (CPU/mem/disk) poller (ignored when `enable_system` is false). |
| `disk_mounts` | `["/"]` | Mount points the system poller reports disk usage for. |
| `ui` | see below | Web UI sub-config. |

The GPU poller shells out to `nvidia-smi`; if it's missing or fails, a single warning is logged and the proxy continues normally.

### `ui` sub-config

| Field | Default | Meaning |
| --- | --- | --- |
| `ui.enabled` | `false` | Serve the web UI at `/__ui/`. |
| `ui.admin_password` | `""` | Required when `ui.enabled` is true — **startup aborts with an error if empty.** Gates UI login and, when the UI is on, also gates `/__journal/*` (a valid session cookie is then required). |
| `ui.session_ttl_seconds` | `43200` (12 h) | Lifetime of an authenticated UI session. |
| `ui.theme` | `"system"` | Default UI theme: `"system"`, `"light"`, or `"dark"`. |
| `ui.show_gpu` | `true` | Default state of the GPU telemetry panel toggle, used until a visitor sets their own preference (persisted per-browser). Has no effect when `enable_gpu` is false (the toggle is hidden entirely). |
| `ui.show_system` | `true` | Default state of the system telemetry panel toggle, used until a visitor sets their own preference (persisted per-browser). Has no effect when `enable_system` is false (the toggle is hidden entirely). |
| `ui.state_colors` | see below | One configurable color per connection state, shared by the state dot, the proxy-connection timeline, and the footer legend. |

#### `ui.state_colors`

Each incoming proxy connection carries a lifecycle `state`, shown in the
connection sub-table as a colored dot (column order: `ts · state · active ·
method · …`). The **same** color set also tints the proxy-connection
timeline segments in the row-expansion panel and the horizontal state
legend at the bottom of the page — configure once, all three follow.
`SUCCESS` renders as a green check and `FAILURE` as a red cross; every
other state renders as a filled dot in its color.

| State | Default | Meaning |
| --- | --- | --- |
| `INIT` | `#9e9e9e` (grey) | Connected / initializing; no action taken yet. |
| `CLASSIFYING` | `#9c27b0` (purple) | Request is being processed through classification. |
| `QUEUED` | `#ff9800` (orange) | Passed classification; resolving destination — all destination models busy, waiting for one to free (global URL tracking). |
| `ROUTING_REQUEST` | `#2196f3` (blue) | Destination resolved; building and sending the proxied request. |
| `ROUTING_RESPONSE` | `#3f51b5` (indigo) | Request sent; awaiting the full upstream response. |
| `SUCCESS` | `#4caf50` (green) | Delivered a `2xx`/`3xx` response; completed successfully. |
| `FAILURE` | `#f44336` (red) | Delivered `≥ 400`, or an error occurred (connection/timeout/exception). |

The four timeline segments use the same configured colors (full
strength, matching the dot): `classifying` (`received→classified`) =
`CLASSIFYING`, `queued` (`classified→routed`) = `QUEUED`, `request`
(`routed→response_started`) = `ROUTING_REQUEST`, `response`
(`response_started→response_completed`) = `ROUTING_RESPONSE`.

`ui.state_colors` is merged per-key against the defaults: supplying only some
states (e.g. `{"SUCCESS": "#00ff00"}`) overrides those and keeps the built-in
color for every unspecified state. Values must be CSS hex colors matching
`^#[0-9A-Fa-f]{3,8}$`; any other value is ignored and the default is used.

### `/__journal/*` API (read-only, GET)

Short-circuits before upstream routing when the journal is enabled.

| Route | Returns |
| --- | --- |
| `/__journal/entries` | JSON array of shallow entries (no headers/bodies) |
| `/__journal/entries/<id>` | Full deep entry, or `404 {"error": ...}` |
| `/__journal/gpu` | Latest GPU sample, or `{}` before the first sample |
| `/__journal/system` | Latest system sample, or `{}` before the first sample |
| `/__journal/stream?topic=…` | Server-Sent Events; `topic` repeatable (e.g. `journal_activity`, `gpu_change`, `system_change`) |

When `ui.enabled` is true, every `/__journal/*` request (including the SSE handshake) requires a valid session cookie obtained via `/__ui/login`.

### Body redaction

Incoming request bodies and upstream response bodies are redacted before storage in the journal. Fields named in `redact_fields` (default `system`, `api_key`, `Authorization`, `api_secret`, `private_key`) are replaced with `"[redacted]"`. Large bodies are truncated. Both knobs are journal-storage concerns and live in `journal.json`.

**Configurable in `journal.json` (top-level fields):**

| Key | Default | Description |
| --- | --- | --- |
| `redact_fields` | `["system", "api_key", "Authorization", "api_secret", "private_key"]` | Body field names replaced with `"[redacted]"` before an entry is stored. Setting this key **replaces** the default list verbatim — the proxy enforces exactly what you specify, so include any built-ins you still want redacted. |
| `max_body_bytes` | `10240` | Max body size kept per journal entry; larger bodies are truncated. Also caps individual content strings inside JSON bodies. |

### `/__ui/*` routes

| Route | Behavior |
| --- | --- |
| `GET /__ui/login` | Login page (open) |
| `POST /__ui/login` | On success → `302 /__ui/` + session cookie; on failure → re-render |
| `POST /__ui/logout` | `302 /__ui/login` + cookie expiry |
| `GET /__ui/` and assets | Dashboard; redirects to `/__ui/login` if unauthenticated |

When `ui.enabled` is false, `/__ui/*` is not intercepted and falls through to upstream routing.

---

## Quick recipes

**Plain rename, one backend (Mode 1):**
```json
{ "default": { "urls": [ { "url": "http://127.0.0.1:11434", "new_model_name": "qwen3:latest" } ] } }
```

**Load-balanced pool with priority (Mode 2):**
```json
{ "default": { "urls": [
  { "url": "http://fast-gpu:11434",  "new_model_name": "qwen3:70b" },
  { "url": "http://spare-gpu:11434", "new_model_name": "qwen3:70b" }
] } }
```

**Classified split (Mode 3):** set `"enabled": true` in `classification.json`, then key `models.json` by `CODING`, `REASONING`, and `default`.

**Add the dashboard:** in `journal.json` set `enabled: true`, then `ui.enabled: true` with a non-empty `ui.admin_password`; browse `http://<host>:<port>/__ui/`.
