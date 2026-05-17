# claude-proxy

[![tests](https://github.com/bjusten/claude-proxy/actions/workflows/tests.yml/badge.svg)](https://github.com/bjusten/claude-proxy/actions/workflows/tests.yml)

A zero-dependency, threaded HTTP proxy that sits between [Claude Code](https://docs.claude.com/en/docs/claude-code) (or any Anthropic / OpenAI-style client) and one or more local model backends such as [Ollama](https://ollama.com), llama.cpp, vLLM, or LM Studio.

```
Claude Code ──► claude-proxy ──► backend(s)
             model='qwen3.6'  → model='Qwen_Qwen3.6-35B-A3B-Q5_K_M'
```

## Why this exists

Claude Code is a genuinely great agentic coding tool — but its default path runs every keystroke of reasoning through Anthropic's hosted Sonnet and Opus models, and that meter never stops running. Meanwhile, open-weight models you can run on your own hardware have gotten *good*: more than capable enough for the bulk of day-to-day coding and reasoning work.

Claude Code talks Anthropic's API and expects to reach Anthropic's servers; local backends listen on different hosts and ports and expect different model names. `claude-proxy` closes that gap. It intercepts every `/v1/*` request, decides which backend should handle it, **rewrites the `model` field** to the name that backend expects, and streams the response straight back — so you keep the Claude Code experience you like while the actual inference runs on models you host yourself, on your workstation, your GPU box, or a small fleet of machines. No per-token bill, no code leaving your network, and full control over which model answers which kind of request — including splitting traffic across several machines, load-balancing a model pool, and routing by *what a request is* rather than *what model the client asked for*.

**Who it's for:**

- Developers who want Claude Code's workflow without the ongoing cost of frontier hosted models.
- People running capable local models (via Ollama, llama.cpp, vLLM, LM Studio) who want to actually *use* them as their daily driver.
- Anyone who needs code and prompts to stay on hardware they control, for privacy or policy reasons.
- Tinkerers who want to split traffic across several machines or route by *what a request is* rather than which model was asked for.

If "I'd use my own models for this if it were just less annoying" sounds familiar, that's the itch this scratches.

## Features

- **Model-based routing** — route by the `model` field in the request body.
- **Backend pools with load balancing** — list several backends per model; each request goes to the one with the fewest in-flight connections, ties broken by list order.
- **Request classification** — optionally classify each request and route on the verdict instead of the requested model. Two methods:
  - **Internal** (default, `method: "internal"`) — a zero-dependency heuristic scorer that classifies into `CODING` or `REASONING` only.
  - **LLM-backed** (`method: "llm"`) — asks an OpenAI-compatible LLM to classify the request using a configurable class map; the heuristic remains the automatic fallback on any LLM failure.
- **`default` catch-all** — any model (or class) with no explicit route falls through to the `default` entry.
- **Model-name rewriting** — the forwarded request's `model` is swapped to each backend's expected name.
- **In-memory activity journal** — optional, off by default. Records proxy activity, GPU/system telemetry, and token usage; exposes a read-only `/__journal/*` HTTP API with SSE streaming. Nothing is written to disk. LLM-classifier calls are recorded too, grouped under a `classification requests` session.
- **Web UI** — optional, password-gated dashboard at `/__ui/` for browsing journal activity and telemetry.
- **Colorized logs** — `--color` tints each log line by upstream URL.
- **Threaded & container-friendly** — `ThreadingMixIn` for concurrency, graceful `SIGTERM` shutdown.
- **Per-request session log** — opt-in (`enable_session_logging` in `proxy.json`). `claude-proxy-sessions.log` with acquire/release lines and live in-flight counts.
- **Proxies `GET`, `POST`, `HEAD`** on `/v1/*`; everything else returns `404`.

See **[CONFIGURATION.md](CONFIGURATION.md)** for every option, broken down per config file, and for a detailed description of the three routing modes.

## Routing modes at a glance

The proxy supports three ways to map an incoming request to a backend (covered in full in [CONFIGURATION.md](CONFIGURATION.md)):

1. **Single destination** — an incoming model maps to exactly one backend entry.
2. **Backend pool** — an incoming model maps to several backends, prioritized by list position and balanced by least active connections.
3. **Classified routing** — the request is classified (`CODING` / `REASONING`) and routed to the backend(s) for that class.

In every mode, the `default` key is the catch-all for anything not explicitly matched.

## Install

Requires **Python 3.11+** and no third-party packages (standard library only).

```bash
git clone https://github.com/bjusten/claude-proxy.git
cd claude-proxy
```

There is nothing to build or `pip install`.

## Run the proxy

```bash
python3 proxy.py                              # listen on :8008 (default)
python3 proxy.py --port 9000                  # custom port
python3 proxy.py --models-config my.json      # custom routing config
python3 proxy.py --color                      # colorize logs per upstream
python3 proxy.py \
    --classification-config classification.json \
    --journal-config journal.json
```

On first run, any missing config file (`models.json`, `proxy.json`, `classification.json`, `journal.json`) is created with safe defaults next to `proxy.py`. Edit them and restart.

| Flag | Default | Purpose |
| --- | --- | --- |
| `--port`, `-p` | `8008` | Port to listen on (binds `0.0.0.0`) |
| `--models-config` | `models.json` | Routing config path |
| `--proxy-config` | `proxy.json` | Proxy settings path |
| `--classification-config` | `classification.json` | Classification config path |
| `--journal-config` | `journal.json` | Journal/UI config path |
| `--color` | off | Colorize log output per upstream URL (TTY + `NO_COLOR` unset) |

## Point Claude Code at it

Set `ANTHROPIC_BASE_URL` to the proxy and launch Claude Code:

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8008 claude
```

Adjust the URL if you changed `--port`. Any client that speaks the Anthropic or OpenAI `/v1/*` API can be pointed at the proxy the same way.

## How it works

1. Proxy loads the routing config (and, if enabled, classification + journal configs).
2. An incoming `/v1/*` request is parsed for its `model` field.
3. The **routing key** is determined: the requested model, or — if classification is enabled — the classifier's verdict (`CODING`/`REASONING`).
4. The key is looked up in `models.json`, falling back to `default`.
5. A backend is selected: the only entry, or (for a pool) the one with the fewest active connections (ties → list order).
6. The body's `model` is rewritten to that backend's `new_model_name`; `Host` and `Content-Length` are fixed up.
7. The request is forwarded and the response streamed back. `/__journal/*` and `/__ui/*` short-circuit before any upstream routing when enabled.

## Logging

Each request logs one stdout line:

```
2026-05-07 10:00:00,000 INFO  POST /v1/messages -> model='qwen3.6' rewritten='qwen3:latest' -> destination='http://127.0.0.1:11434'
```

With classification on, the verdict and scores are included:

```
POST /v1/messages -> model='claude-opus-4-7' classified='CODING' (code=87.4 reasoning=3.1) rewritten='coding-model' -> destination='http://coding-host:8083'
```

When `enable_session_logging` is set to `true` in `proxy.json` (default `false`), a separate `claude-proxy-sessions.log` (in the working directory) gets an `ACQUIRED` and `RELEASED` line per request, including the live in-flight `active=` count, backend index, URL, and rewritten model — useful for watching pool balancing in real time.

## License

[MIT](LICENSE) — do whatever you want with this code; it's provided as-is with no warranty or liability.
