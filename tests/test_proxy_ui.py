"""End-to-end tests for the Journal UI wiring in ClaudeProxyHandler.

Mirrors `test_proxy_journal.py` patterns. Covers:
  - /__ui/* short-circuits when UI is enabled
  - /__ui/* falls through to upstream when UI is disabled
  - Login → authed index → logout flow
  - /__journal/* (JSON + SSE) requires a session cookie when UI is enabled
  - /__journal/* stays open when UI is disabled
"""

import http.client
import json
import socket
import threading
import unittest
import unittest.mock as mock
from urllib.error import URLError

from proxy import ClaudeProxyHandler, ThreadingHTTPServer, make_proxy_launched_entry
from journal.auth import AuthStore
from journal.bus import EventBus
from journal.store import JournalStore


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        return s.getsockname()[1]


def _request(port, method, path, *, headers=None, body=None, timeout=5):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def _open(port, method, path, *, headers=None, body=None, timeout=5):
    """Send a request and return the open response (caller closes)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    return conn, resp


class _UIProxyFixture:
    """Bring up a live ClaudeProxyHandler with a JournalStore and optional UI."""

    def __init__(self, *, ui_enabled, password="hunter2", ttl=3600):
        self.port = _free_port()
        self.store = JournalStore()
        entry = make_proxy_launched_entry(self.port)
        self.store.append(entry, pinned=True)
        self.bus = EventBus()
        self.auth_store = AuthStore(password, ttl) if ui_enabled else None

        config = {"default": {"urls": [{"url": "http://127.0.0.1:9999"}]}}
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", self.port),
            lambda *args, **kw: ClaudeProxyHandler(
                *args,
                models=config,
                journal_store=self.store,
                journal_bus=self.bus,
                auth_store=self.auth_store,
                **kw,
            ),
        )
        self.t = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.t.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def login(self, password="hunter2"):
        """Authenticate: GET login for CSRF, then POST with token. Returns session token."""
        # GET /__ui/login → CSRF cookie
        _, get_hdrs, _ = _request(self.port, "GET", "/__ui/login")
        csrf = self._extract_cookie(get_hdrs, "csrf")
        # POST with CSRF token in both form body AND cookie header
        _, post_hdrs, _ = _request(
            self.port, "POST", "/__ui/login",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": f"csrf={csrf}",
            },
            body=f"password={password}&csrf_token={csrf}",
        )
        return self._extract_cookie(post_hdrs, "session")

    def _extract_cookie(self, headers, name):
        """Pull a cookie value from possibly-multiple Set-Cookie headers."""
        raw = headers.get("Set-Cookie", "")
        if not raw:
            return ""
        for part in raw.split("; "):
            if part.startswith(name + "="):
                return part[len(name) + 1:].split(";")[0].strip()
        return ""


class TestUiEnabledFlow(unittest.TestCase):
    def setUp(self):
        self.fx = _UIProxyFixture(ui_enabled=True, password="hunter2")

    def tearDown(self):
        self.fx.stop()

    def test_get_index_without_cookie_redirects_to_login(self):
        status, headers, _ = _request(self.fx.port, "GET", "/__ui/")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/__ui/login")

    def test_get_login_returns_200_login_html(self):
        status, headers, body = _request(self.fx.port, "GET", "/__ui/login")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"Sign in", body)

    def test_post_login_wrong_password_renders_login_no_cookie(self):
        status, headers, body = _request(
            self.fx.port, "POST", "/__ui/login",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body="password=nope",
        )
        self.assertEqual(status, 200)
        self.assertNotIn("Set-Cookie", {k.lower(): None for k in headers}.keys() and headers)
        # Belt-and-suspenders: header may be present case-folded
        cookie_header = next(
            (v for k, v in headers.items() if k.lower() == "set-cookie"), None,
        )
        self.assertIsNone(cookie_header)
        self.assertIn(b"Incorrect password", body)

    def test_full_login_index_logout_flow(self):
        # 1. Authenticate → session cookie
        token = self.fx.login()
        self.assertTrue(len(token) > 0)

        # 2. Use the cookie to fetch the authed index
        status, hdrs, body = _request(
            self.fx.port, "GET", "/__ui/",
            headers={"Cookie": f"session={token}"},
        )
        self.assertEqual(status, 200)
        self.assertIn("text/html", hdrs["Content-Type"])
        # Index now mounts the activity list scaffold; assert on structure
        # rather than placeholder copy.
        self.assertIn(b'id="rows"', body)

        # 3. Authed asset fetch works
        status, hdrs, body = _request(
            self.fx.port, "GET", "/__ui/style.css",
            headers={"Cookie": f"session={token}"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(hdrs["Content-Type"], "text/css; charset=utf-8")

        # 3b. Public asset served without auth (login page CSS)
        status, hdrs, body = _request(self.fx.port, "GET", "/__ui/style.css")
        self.assertEqual(status, 200)
        self.assertEqual(hdrs["Content-Type"], "text/css; charset=utf-8")

        # 4. Unauthed non-public asset fetch redirects
        status, hdrs, _ = _request(self.fx.port, "GET", "/__ui/app.js")
        self.assertEqual(status, 302)
        self.assertEqual(hdrs["Location"], "/__ui/login")

        # 5. Log out → 302 + cookie expiry
        status, hdrs, _ = _request(
            self.fx.port, "POST", "/__ui/logout",
            headers={"Cookie": f"session={token}"},
        )
        self.assertEqual(status, 302)
        self.assertEqual(hdrs["Location"], "/__ui/login")
        self.assertIn("Max-Age=0", hdrs["Set-Cookie"])

        # 6. Token is now invalid; using it again redirects to login
        status, hdrs, _ = _request(
            self.fx.port, "GET", "/__ui/",
            headers={"Cookie": f"session={token}"},
        )
        self.assertEqual(status, 302)
        self.assertEqual(hdrs["Location"], "/__ui/login")

    def test_unknown_path_under_ui_returns_404(self):
        # First authenticate so we hit the asset path, not the auth gate.
        token = self.fx.login()
        status, _, _ = _request(
            self.fx.port, "GET", "/__ui/no-such-asset.js",
            headers={"Cookie": f"session={token}"},
        )
        self.assertEqual(status, 404)

    def test_journal_entries_without_cookie_returns_401(self):
        status, _, body = _request(self.fx.port, "GET", "/__journal/entries")
        self.assertEqual(status, 401)
        self.assertIn(b"unauthorized", body)

    def test_journal_entries_with_valid_cookie_returns_list(self):
        token = self.fx.login()
        status, _, body = _request(
            self.fx.port, "GET", "/__journal/entries",
            headers={"Cookie": f"session={token}"},
        )
        self.assertEqual(status, 200)
        entries = json.loads(body)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["type"], "proxy_launched")

    def test_journal_stream_without_cookie_returns_401(self):
        status, _, body = _request(
            self.fx.port, "GET", "/__journal/stream?topic=journal_activity",
        )
        self.assertEqual(status, 401)
        self.assertIn(b"unauthorized", body)

    def test_journal_stream_with_valid_cookie_streams(self):
        token = self.fx.login()
        conn, resp = _open(
            self.fx.port, "GET", "/__journal/stream?topic=journal_activity",
            headers={"Cookie": f"session={token}"},
            timeout=2,
        )
        try:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.getheader("Content-Type"), "text/event-stream")
        finally:
            conn.close()


class TestUiDisabledFallthrough(unittest.TestCase):
    """When ui.enabled is false: /__ui/* is rejected with 404 (only /v1/*
    is proxied). Journal paths remain open when the journal store is enabled."""

    def setUp(self):
        self.fx = _UIProxyFixture(ui_enabled=False)

    def tearDown(self):
        self.fx.stop()

    def test_ui_path_rejected_when_disabled(self):
        # /__ui/* is not under /v1/*, so it must return 404 — not fall through
        # to upstream (the old behavior).
        status, _, _ = _request(self.fx.port, "GET", "/__ui/")
        self.assertEqual(status, 404)

    def test_journal_entries_open_without_cookie(self):
        status, _, body = _request(self.fx.port, "GET", "/__journal/entries")
        self.assertEqual(status, 200)
        entries = json.loads(body)
        self.assertEqual(entries[0]["type"], "proxy_launched")


if __name__ == "__main__":
    unittest.main()
