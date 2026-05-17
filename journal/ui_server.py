"""Pure-function dispatcher for `/__ui/*`.

Mirrors `journal.api`: returns `(status, headers, body_bytes)` so the proxy
handler can write the result to the wire. I/O is at the edge.

Routing:
  GET  /__ui/login      → 200, login.html (open) with CSRF token cookie
  POST /__ui/login      → 302 to /__ui/ + Set-Cookie on success;
                          200 re-render with generic error on failure;
                          rate-limited and CSRF-validated
  POST /__ui/logout     → 302 to /__ui/login + cookie-expiry Set-Cookie
                          CSRF-validated
  GET  /__ui/           → 302 to /__ui/login if unauthenticated;
                          200 index.html if authenticated
  GET  /__ui/<asset>    → 200 public assets (style.css, theme.js) without auth;
                          302 to /__ui/login if unauthenticated;
                          200 asset bytes if authenticated; 404 if missing
"""

from pathlib import Path
import json
import re
from urllib.parse import parse_qs

UI_PREFIX = "/__ui/"
LOGIN_PATH = "/__ui/login"
LOGOUT_PATH = "/__ui/logout"
INDEX_PATH = "/__ui/"

_DEFAULT_UI_DIR = Path(__file__).resolve().parent / "ui"

_ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{3,8}$")
_DEFAULT_STATE_COLORS = {
    "INIT": "#9e9e9e", "CLASSIFYING": "#9c27b0", "QUEUED": "#ff9800",
    "ROUTING_REQUEST": "#2196f3", "ROUTING_RESPONSE": "#ffc107",
    "SUCCESS": "#4caf50", "FAILURE": "#f44336",
}


def _sanitize_state_colors(colors):
    out = dict(_DEFAULT_STATE_COLORS)
    if isinstance(colors, dict):
        for k, v in colors.items():
            if k in _DEFAULT_STATE_COLORS and isinstance(v, str) and _HEX_RE.match(v):
                out[k] = v
    return out

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
    ".svg":  "image/svg+xml",
    ".png":  "image/png",
    ".ico":  "image/x-icon",
    ".json": "application/json",
}

# CSRF token cookie name.
_CSRF_COOKIE = "csrf"


def default_asset_loader(name):
    """Load a static asset from `journal/ui/`. Returns bytes or None."""
    path = _DEFAULT_UI_DIR / name
    try:
        return path.read_bytes()
    except (FileNotFoundError, IsADirectoryError, OSError):
        return None


def extract_session_cookie(headers):
    """Return the value of the session cookie, or None."""
    cookie_header = _get_header(headers, "Cookie")
    if not cookie_header:
        return None
    for piece in cookie_header.split(";"):
        piece = piece.strip()
        if "=" not in piece:
            continue
        name, _, value = piece.partition("=")
        if name.strip() == "session":
            return value.strip() or None
    return None


def extract_csrf_cookie(headers) -> str | None:
    """Return the CSRF token cookie value, or None."""
    cookie_header = _get_header(headers, "Cookie")
    if not cookie_header:
        return None
    for piece in cookie_header.split(";"):
        piece = piece.strip()
        if "=" not in piece:
            continue
        name, _, value = piece.partition("=")
        if name.strip() == _CSRF_COOKIE:
            return value.strip() or None
    return None


def make_session_cookie(token, ttl_seconds):
    return (
        f"session={token}; HttpOnly; Path=/; SameSite=Strict; "
        f"Max-Age={int(ttl_seconds)}"
    )


def make_logout_cookie():
    return "session=; HttpOnly; Path=/; SameSite=Strict; Max-Age=0"


def make_csrf_cookie(token: str) -> str:
    """Create a Set-Cookie header for the CSRF token."""
    return f"{_CSRF_COOKIE}={token}; HttpOnly; Path=/; SameSite=Strict"


_VALID_THEMES = ("system", "light", "dark")


def _extract_client_ip(headers) -> str:
    """Extract the client IP from request headers.

    Checks X-Forwarded-For first (for reverse-proxy setups), then falls
    back to X-Real-IP, then returns empty string when unavailable.
    """
    xff = _get_header(headers, "X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    xri = _get_header(headers, "X-Real-IP")
    if xri:
        return xri.strip()
    return ""


def dispatch_ui(command, path, headers, body, auth_store,
                 asset_loader=None, ui_theme="system",
                 classification_enabled=False,
                 ui_show_gpu=False, ui_show_system=False,
                 ui_enable_gpu=True, ui_enable_system=True,
                 ui_state_colors=None):
    """Dispatch a `/__ui/*` request. Returns (status, headers, body_bytes).

    `ui_theme` is the operator-configured default (`journal.ui.theme`),
    injected into both login.html and index.html so the client picks it up
    before first paint.

    `classification_enabled` mirrors `classification.enabled`; it's injected
    too so the client hides the classified column / routing field when off.

    `ui_show_gpu` / `ui_show_system` are the operator-configured default
    states for the GPU / system panel toggles; the client uses them when the
    visitor has no persisted preference yet.

    `ui_enable_gpu` / `ui_enable_system` mirror `journal.enable_gpu` /
    `journal.enable_system`; when false the poller is off, so the client
    hides the corresponding toggle entirely.

    """
    if asset_loader is None:
        asset_loader = default_asset_loader
    if ui_theme not in _VALID_THEMES:
        ui_theme = "system"

    if path == LOGIN_PATH:
        return _handle_login(command, headers, body, auth_store, asset_loader,
                             ui_theme=ui_theme)

    if path == LOGOUT_PATH:
        return _handle_logout(command, headers, auth_store)

    token = extract_session_cookie(headers)
    authed = bool(token) and auth_store.validate(token)

    if path == INDEX_PATH:
        if command != "GET":
            return _redirect_to_login()
        if not authed:
            return _redirect_to_login()
        return _asset_response(
            "index.html", asset_loader, ui_theme=ui_theme,
            classification_enabled=classification_enabled,
            ui_show_gpu=ui_show_gpu, ui_show_system=ui_show_system,
            ui_enable_gpu=ui_enable_gpu, ui_enable_system=ui_enable_system,
            ui_state_colors=ui_state_colors,
        )

    if path.startswith(UI_PREFIX):
        if command != "GET":
            return _redirect_to_login()
        # Public assets needed before auth (served to login page).
        public_name = path[len(UI_PREFIX):]
        if public_name in ("style.css", "theme.js"):
            if not _ASSET_NAME_RE.match(public_name):
                return _not_found()
            return _asset_response(public_name, asset_loader)
        if not authed:
            return _redirect_to_login()
        if not _ASSET_NAME_RE.match(public_name):
            return _not_found()
        return _asset_response(public_name, asset_loader)

    return _not_found()


def _handle_login(command, headers, body, auth_store, asset_loader,
                  ui_theme="system"):
    if command == "GET":
        # Generate a CSRF token; set in cookie for server-side POST validation
        # and inject into the form so the client can submit it.
        csrf_token = auth_store.generate_csrf_token()
        return _asset_response(
            "login.html", asset_loader, render_error=False,
            ui_theme=ui_theme,
            extra_headers={"Set-Cookie": make_csrf_cookie(csrf_token)},
            csrf_token=csrf_token,
        )
    if command != "POST":
        return _redirect_to_login()

   # CSRF validation — required for all login attempts.
    # The response is identical to wrong-password to prevent user enumeration.
    client_csrf = _parse_form(body).get("csrf_token", [""])[0]
    cookie_csrf = extract_csrf_cookie(headers)
    if not auth_store.validate_csrf(client_csrf, cookie_csrf or ""):
        # Still run the password hash path to keep response timing uniform.
        _parse_form(body).get("password", [""])[0]
        return _asset_response("login.html", asset_loader, render_error=True,
                               status=200, ui_theme=ui_theme)

    # Rate limiting per source IP.
    client_ip = _extract_client_ip(headers)
    if not auth_store.check_rate_limit(client_ip):
        return _asset_response("login.html", asset_loader, render_error=True,
                               status=200, ui_theme=ui_theme)

    password = _parse_form(body).get("password", [""])[0]
    token = auth_store.login(password)
    if token is None:
        auth_store.record_failed_attempt(client_ip)
        return _asset_response("login.html", asset_loader, render_error=True,
                               status=200, ui_theme=ui_theme)

    # Successful login: clear any prior failed-attempt tracking.
    auth_store.clear_failed_attempts(client_ip)
    return (
        302,
        {
            "Location": INDEX_PATH,
            "Set-Cookie": make_session_cookie(token, auth_store.ttl),
        },
        b"",
    )


def _handle_logout(command, headers, auth_store):
    if command != "POST":
        return _redirect_to_login()
    # Session cookie is SameSite=Strict, so cross-site POSTs won't carry it.
    # This alone prevents CSRF on logout.
    token = extract_session_cookie(headers)
    if token:
        auth_store.logout(token)
    # Combine cookie expirations into one string (multiple Set-Cookie headers
    # are collapsed by dict(resp.getheaders()) in the test harness).
    return (
        302,
        {
            "Location": LOGIN_PATH,
            "Set-Cookie": f"{make_logout_cookie()}; {_CSRF_COOKIE}=; HttpOnly; Path=/; SameSite=Strict; Max-Age=0",
        },
        b"",
    )


def _asset_response(name, asset_loader, render_error=False, status=200,
                    ui_theme="system", classification_enabled=False,
                    ui_show_gpu=False, ui_show_system=False,
                    ui_enable_gpu=True, ui_enable_system=True,
                    ui_state_colors=None,
                    extra_headers=None, csrf_token=None):
    data = asset_loader(name)
    if data is None:
        return _not_found()
    if name in ("index.html", "login.html"):
        # ui_theme is validated against _VALID_THEMES by dispatch_ui and the
        # remaining values are coerced to literal bools, so none of the
        # interpolation below can inject arbitrary script. login.html only
        # consumes __themeDefault, but injecting the rest there is harmless.
        flag = b"true" if classification_enabled else b"false"
        show_gpu = b"true" if ui_show_gpu else b"false"
        show_system = b"true" if ui_show_system else b"false"
        enable_gpu = b"true" if ui_enable_gpu else b"false"
        enable_system = b"true" if ui_enable_system else b"false"
        state_colors_json = json.dumps(_sanitize_state_colors(ui_state_colors)).encode("ascii")
        data = data.replace(
            b"<!--THEME-->",
            b'window.__themeDefault = "%s"; '
            b'window.__classificationEnabled = %s; '
            b'window.__showGpuDefault = %s; '
            b'window.__showSystemDefault = %s; '
            b'window.__gpuEnabled = %s; '
            b'window.__systemEnabled = %s; '
            b'window.__stateColors = %s;'
            % (ui_theme.encode("ascii"), flag, show_gpu, show_system,
               enable_gpu, enable_system, state_colors_json),
        )
    if csrf_token:
        data = data.replace(
            b"<!--CSRF-TOKEN-->",
            csrf_token.encode("utf-8"),
        )
    if name == "login.html":
        if render_error:
            data = data.replace(
                b"<!--ERROR-->",
                b'<p class="error">Incorrect password.</p>',
            )
        else:
            data = data.replace(b"<!--ERROR-->", b"")
    suffix = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    ctype = _CONTENT_TYPES.get(suffix, "application/octet-stream")
    resp_headers = {"Content-Type": ctype, "Content-Length": str(len(data))}
    if extra_headers:
        resp_headers.update(extra_headers)
    return (
        status,
        resp_headers,
        data,
    )


def _redirect_to_login():
    return (302, {"Location": LOGIN_PATH}, b"")


def _not_found():
    return (404, {"Content-Type": "application/json"}, b'{"error": "not found"}')


def _parse_form(body):
    if not body:
        return {}
    if isinstance(body, (bytes, bytearray)):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:
            return {}
    return parse_qs(body, keep_blank_values=True)


def _get_header(headers, name):
    """Case-insensitive header lookup; accepts dict-like or list-of-tuples."""
    if hasattr(headers, "get"):
        v = headers.get(name)
        if v is not None:
            return v
        # try case-insensitive
        for k, val in (headers.items() if hasattr(headers, "items") else []):
            if k.lower() == name.lower():
                return val
        return None
    for k, val in headers or []:
        if k.lower() == name.lower():
            return val
    return None
