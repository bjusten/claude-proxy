"""Journal HTTP API: path-prefix dispatch for `/__journal/*`.

`dispatch(command, path, store)` is a pure function returning `(status,
headers, body_bytes)`. The proxy handler is responsible for writing the result
to the wire. Keeping I/O at the edge makes the routes trivially unit-testable.

`stream(handler, store, bus, topics, ...)` writes Server-Sent Events directly
to the handler's wfile until the client disconnects or the supplied stop
event is set. SSE prelude includes `retry: 5000`; an idle keepalive comment
is emitted every `keepalive_interval` seconds (15 s by default).

The stream validates the session periodically (every `reauth_interval`
seconds, default 30 s). If validation fails the stream sends a
`session_expired` event and terminates, forcing the client to re-authenticate.
"""

import json
import time

JSON_HEADERS = {"Content-Type": "application/json"}

_ENTRIES_PREFIX = "/__journal/entries/"


def _json_default(obj):
    """Render non-JSON-serializable values. Bytes → utf-8 (errors=replace)."""
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", errors="replace")
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _dumps(obj):
    return json.dumps(obj, default=_json_default).encode("utf-8")


def dispatch(command, path, store, gpu_provider=None, system_provider=None):
    if command == "GET" and path == "/__journal/entries":
        entries = store.list_shallow()
        body = _dumps(entries)
        return 200, dict(JSON_HEADERS), body

    if command == "GET" and path.startswith(_ENTRIES_PREFIX):
        entry_id = path[len(_ENTRIES_PREFIX):]
        entry = store.get_deep(entry_id)
        if entry is not None:
            body = _dumps(entry)
            return 200, dict(JSON_HEADERS), body

    if command == "GET" and path == "/__journal/gpu":
        latest = gpu_provider() if gpu_provider is not None else {}
        body = _dumps(latest if latest else {})
        return 200, dict(JSON_HEADERS), body

    if command == "GET" and path == "/__journal/system":
        latest = system_provider() if system_provider is not None else {}
        body = _dumps(latest if latest else {})
        return 200, dict(JSON_HEADERS), body

    body = _dumps({"error": "not found"})
    return 404, dict(JSON_HEADERS), body


def stream(handler, store, bus, topics, *,
           keepalive_interval=15.0, poll_interval=0.5,
           reauth_interval=30.0, stop_event=None, now=None,
           auth_validator=None):
    """Stream `journal_activity`-style SSE events to the handler's wfile.

    `auth_validator` is called with the handler every `reauth_interval`
    seconds. If it returns False the stream sends a ``session_expired``
    event and terminates. This prevents stale streams from leaking data
    after the user logs out or their session expires.
    """
    clock = now if now is not None else time.monotonic

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.end_headers()

    def _write(data):
        try:
            handler.wfile.write(data)
            flush = getattr(handler.wfile, "flush", None)
            if flush is not None:
                flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    if not _write(b"retry: 5000\n\n"):
        return

    last_write = clock()
    last_auth_check = 0.0
    with bus.subscribe(list(topics)) as sub:
        while True:
            if stop_event is not None and stop_event.is_set():
                return

            # Periodic session re-authentication.
            now_mono = clock()
            if (auth_validator is not None
                    and now_mono - last_auth_check >= reauth_interval):
                last_auth_check = now_mono
                if not auth_validator(handler):
                    block = b"event: session_expired\n\n"
                    if not _write(block):
                        return
                    return

            # Drain whatever events are queued without blocking.
            drained = False
            while True:
                with sub.lock:
                    if not sub.queue:
                        break
                    topic, event = sub.queue.popleft()
                block = (
                    b"event: " + topic.encode("utf-8") + b"\n"
                    + b"data: " + _dumps(event) + b"\n\n"
                )
                if not _write(block):
                    return
                last_write = clock()
                drained = True
            if drained:
                continue
            # Idle path: park briefly, then emit keepalive if interval passed.
            sub.event.wait(timeout=poll_interval)
            sub.event.clear()
            if clock() - last_write >= keepalive_interval:
                if not _write(b": keepalive\n\n"):
                    return
                last_write = clock()
