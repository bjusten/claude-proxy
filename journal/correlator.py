"""Session correlation: derive a stable session id from request metadata.

Pure function. Header priority order:
  anthropic-session-id → x-session-id → anthropic-trace-id → x-request-id
  → fallback: deterministic hash of (user-agent, peer)
"""

import hashlib
import json


_PRIORITY = (
    "anthropic-session-id",
    "x-session-id",
    "anthropic-trace-id",
    "x-request-id",
)


def _session_id_from_body(request_data):
    """Extract session_id from request body metadata if present.

    metadata.user_id may be a JSON-encoded string containing {session_id}.
    """
    if not isinstance(request_data, dict):
        return None
    metadata = request_data.get("metadata")
    if not isinstance(metadata, dict):
        return None
    user_id = metadata.get("user_id")
    if isinstance(user_id, str):
        try:
            parsed = json.loads(user_id)
            if isinstance(parsed, dict):
                return parsed.get("session_id")
        except (json.JSONDecodeError, ValueError):
            pass
    elif isinstance(user_id, dict):
        return user_id.get("session_id")
    return None


def session_id_for(headers, peer, request_data=None):
    """Return the session id for a request.

    Priority: request body metadata > HTTP headers > hash of (user-agent, peer).
    """
    if request_data and (body_id := _session_id_from_body(request_data)):
        return body_id
    lowered = {k.lower(): v for k, v in headers.items()}
    for key in _PRIORITY:
        if key in lowered:
            return lowered[key]
    ua = lowered.get("user-agent", "")
    digest = hashlib.sha256(f"{ua}|{peer}".encode("utf-8")).hexdigest()[:16]
    return f"anon-{digest}"
