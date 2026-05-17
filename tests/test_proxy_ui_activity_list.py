"""End-to-end tests for the Journal UI activity list.

Brings up a live ClaudeProxyHandler with UI + journal enabled and a real
in-process upstream. Asserts the data path that drives the activity list:
shallow seed via /__journal/entries, append+update lifecycle via the
journal_activity SSE topic, and the static JS assets the page loads.
"""

import http.client
import json
import re
import select
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as _UpstreamServer

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
    """Send a request and return the open connection + response."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    return conn, resp


class _OkUpstreamHandler(BaseHTTPRequestHandler):
    """Mock upstream that always returns 200 {'ok': true}."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length:
            self.rfile.read(length)
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args, **kwargs):  # silence
        pass


class _ActivityFixture:
    """Live ClaudeProxyHandler with UI + journal + a real upstream."""

    def __init__(self, *, password="hunter2", ttl=3600):
        # Upstream first so we know its port.
        self.up_port = _free_port()
        self.up = _UpstreamServer(("127.0.0.1", self.up_port), _OkUpstreamHandler)
        self.up_t = threading.Thread(target=self.up.serve_forever, daemon=True)
        self.up_t.start()

        # Journal + auth.
        self.bus = EventBus()
        self.store = JournalStore(bus=self.bus)
        self.auth_store = AuthStore(password, ttl)

        # Proxy server.
        self.port = _free_port()
        self.store.append(make_proxy_launched_entry(self.port), pinned=True)
        config = {
            "model-a": {
                "urls": [{
                    "url": f"http://127.0.0.1:{self.up_port}",
                    "new_model_name": "llama3:70b",
                }],
            },
        }
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

    def login(self, password="hunter2"):
        # Step 1: GET /__ui/login to pick up the CSRF cookie.
        _, get_hdrs, _ = _request(self.port, "GET", "/__ui/login")
        csrf = self._extract_cookie(get_hdrs, "csrf")
        # Step 2: POST back with CSRF token in form body AND cookie header.
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
                # Cookie value ends at next "; " or end of string.
                return part[len(name) + 1:].split(";")[0].strip()
        return ""

    def post_traffic(self, n=1):
        """Fire `n` synthetic POSTs through the proxy."""
        for _ in range(n):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            try:
                conn.request(
                    "POST", "/v1/messages",
                    body=json.dumps({"model": "model-a", "messages": []}).encode(),
                    headers={"Content-Type": "application/json"},
                )
                r = conn.getresponse()
                r.read()
            finally:
                conn.close()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.up.shutdown()
        self.up.server_close()


class TestSeedEndpoint(unittest.TestCase):

    def setUp(self):
        self.fx = _ActivityFixture()

    def tearDown(self):
        self.fx.stop()

    def test_seed_returns_shallow_activity_with_cookie(self):
        token = self.fx.login()
        self.fx.post_traffic(n=3)

        # Allow the response phase to land in the journal.
        deadline = time.monotonic() + 2.0
        entries = []
        while time.monotonic() < deadline:
            status, _, body = _request(
                self.fx.port, "GET", "/__journal/entries",
                headers={"Cookie": f"session={token}"},
            )
            self.assertEqual(status, 200)
            entries = json.loads(body)
            conns = [e for e in entries if e["type"] == "proxy_connection"]
            if len(conns) == 3 and all(c.get("status") == 200 for c in conns):
                break
            time.sleep(0.05)

        conns = [e for e in entries if e["type"] == "proxy_connection"]
        self.assertEqual(len(conns), 3)
        for c in conns:
            self.assertEqual(c["method"], "POST")
            self.assertEqual(c["path"], "/v1/messages")
            self.assertEqual(c["status"], 200)
            self.assertEqual(c["bytes"], len(b'{"ok":true}'))
            self.assertIn("ts", c)
            self.assertIn("destination_url", c)

        launched = [e for e in entries if e["type"] == "proxy_launched"]
        self.assertEqual(len(launched), 1)


def _read_sse_events(resp, until, timeout=3.0):
    """Read SSE bytes from an open response until `until(events)` is true.

    Returns the accumulated list of {topic, data} dicts. The HTTP response
    must already have been read up through end-of-headers. Raises
    AssertionError if the timeout elapses first.
    """
    buf = bytearray()
    events = []
    deadline = time.monotonic() + timeout
    # Get the underlying socket from the response
    sock = resp.fp.raw._sock
    sock.setblocking(False)  # Non-blocking socket
    try:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Use select to wait for data with timeout
            ready, _, _ = select.select([sock], [], [], min(remaining, 0.1))
            if not ready:
                # No data available; check if we have enough events
                if until(events):
                    return events
                continue
            try:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buf.extend(chunk)
            except BlockingIOError:
                continue

            while b"\n\n" in buf:
                raw, _, rest = buf.partition(b"\n\n")
                buf = bytearray(rest)
                topic = None
                data = None
                for line in raw.split(b"\n"):
                    if line.startswith(b"event: "):
                        topic = line[len(b"event: "):].decode("utf-8")
                    elif line.startswith(b"data: "):
                        data = json.loads(line[len(b"data: "):].decode("utf-8"))
                if topic is not None and data is not None:
                    events.append({"topic": topic, "data": data})
            if until(events):
                return events
    finally:
        sock.setblocking(True)
    raise AssertionError(
        f"SSE timeout after {timeout}s; events so far: {events}"
    )


class TestActivitySseLifecycle(unittest.TestCase):

    def setUp(self):
        self.fx = _ActivityFixture()

    def tearDown(self):
        self.fx.stop()

    def test_sse_delivers_append_then_update_for_same_id(self):
        token = self.fx.login()

        # Open SSE first so we capture both append and update events.
        sse_conn, sse_resp = _open(
            self.fx.port, "GET",
            "/__journal/stream?topic=journal_activity",
            headers={"Cookie": f"session={token}"},
            timeout=5,
        )
        try:
            self.assertEqual(sse_resp.status, 200)
            self.assertEqual(
                sse_resp.getheader("Content-Type"),
                "text/event-stream",
            )

            # Fire one synthetic request; this should produce an append
            # event (status=None) then a later update event (status=200)
            # both keyed by the same id.
            self.fx.post_traffic(n=1)

            def _has_append_and_update(events):
                ids_with_null = {e["data"]["id"] for e in events
                                 if e["data"].get("type") == "proxy_connection"
                                 and e["data"].get("status") is None}
                ids_with_200 = {e["data"]["id"] for e in events
                                if e["data"].get("type") == "proxy_connection"
                                and e["data"].get("status") == 200}
                return bool(ids_with_null & ids_with_200)

            events = _read_sse_events(sse_resp, _has_append_and_update,
                                      timeout=3.0)

            conn_events = [e for e in events
                           if e["data"].get("type") == "proxy_connection"]
            ids = {e["data"]["id"] for e in conn_events}
            self.assertEqual(len(ids), 1, "all conn events share an id")

            statuses = [e["data"].get("status") for e in conn_events]
            self.assertIn(None, statuses, "append event has status=None")
            self.assertIn(200, statuses, "update event has status=200")
        finally:
            sse_conn.close()


class TestJsAssetsServed(unittest.TestCase):

    def setUp(self):
        self.fx = _ActivityFixture()

    def tearDown(self):
        self.fx.stop()

    def test_app_js_served_with_cookie(self):
        token = self.fx.login()
        status, hdrs, body = _request(
            self.fx.port, "GET", "/__ui/app.js",
            headers={"Cookie": f"session={token}"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            hdrs["Content-Type"],
            "application/javascript; charset=utf-8",
        )
        self.assertGreater(len(body), 0)

    def test_sse_js_served_with_cookie(self):
        token = self.fx.login()
        status, hdrs, body = _request(
            self.fx.port, "GET", "/__ui/sse.js",
            headers={"Cookie": f"session={token}"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            hdrs["Content-Type"],
            "application/javascript; charset=utf-8",
        )
        self.assertGreater(len(body), 0)

    def test_app_js_redirects_without_cookie(self):
        status, hdrs, _ = _request(self.fx.port, "GET", "/__ui/app.js")
        self.assertEqual(status, 302)
        self.assertEqual(hdrs["Location"], "/__ui/login")


class TestIndexHtmlScaffold(unittest.TestCase):

    def setUp(self):
        self.fx = _ActivityFixture()

    def tearDown(self):
        self.fx.stop()

    def test_index_includes_activity_table_and_app_script(self):
        token = self.fx.login()
        status, hdrs, body = _request(
            self.fx.port, "GET", "/__ui/",
            headers={"Cookie": f"session={token}"},
        )
        self.assertEqual(status, 200)
        self.assertIn("text/html", hdrs["Content-Type"])
        # Required scaffold elements.
        self.assertIn(b'<tbody id="rows">', body)
        self.assertIn(b'id="empty"', body)
        self.assertIn(b'id="conn"', body)
        self.assertIn(
            b'<script type="module" src="/__ui/app.js">',
            body,
        )

    def test_index_has_no_global_connection_header(self):
        token = self.fx.login()
        status, _hdrs, body = _request(
            self.fx.port, "GET", "/__ui/",
            headers={"Cookie": f"session={token}"},
        )
        self.assertEqual(status, 200)
        # The connection column header must no longer live in the page <thead>;
        # it now lives inside each expanded session's nested conn-table.
        self.assertNotIn(b'connection-header', body)
        # Session header is still present.
        self.assertIn(b'session-header', body)

    def test_session_table_js_builds_nested_conn_table(self):
        token = self.fx.login()
        status, _hdrs, body = _request(
            self.fx.port, "GET", "/__ui/session-table.js",
            headers={"Cookie": f"session={token}"},
        )
        self.assertEqual(status, 200)
        src = body.decode("utf-8")
        self.assertIn("conn-table", src)
        self.assertIn("session-conns-row", src)
        self.assertNotIn("toggleConnectionHeader", src)
        self.assertNotIn("connectionHeaderRow", src)

    def test_expand_js_uses_nested_conn_table_for_colspan(self):
        token = self.fx.login()
        status, _hdrs, body = _request(
            self.fx.port, "GET", "/__ui/expand.js",
            headers={"Cookie": f"session={token}"},
        )
        self.assertEqual(status, 200)
        src = body.decode("utf-8")
        self.assertIn("conn-table", src)
        self.assertNotIn("connection-header", src)


class TestClassificationGating(unittest.TestCase):
    """Classification chrome is hidden unless the operator enabled it.

    The fixture runs with classification off (the default), so the served
    index.html must advertise the flag as false and the JS must gate the
    `classified` column and routing field on `window.__classificationEnabled`.
    """

    def setUp(self):
        self.fx = _ActivityFixture()

    def tearDown(self):
        self.fx.stop()

    def _get(self, path):
        token = self.fx.login()
        status, _hdrs, body = _request(
            self.fx.port, "GET", path,
            headers={"Cookie": f"session={token}"},
        )
        self.assertEqual(status, 200)
        return body.decode("utf-8")

    def test_index_advertises_classification_disabled_by_default(self):
        self.assertIn("window.__classificationEnabled = false;", self._get("/__ui/"))

    def test_session_table_gates_classified_column_on_flag(self):
        src = self._get("/__ui/session-table.js")
        self.assertIn("__classificationEnabled", src)
        # The classified column is filtered out, not unconditionally listed.
        self.assertIn("classificationEnabled", src)

    def test_expand_gates_classified_routing_field_on_flag(self):
        src = self._get("/__ui/expand.js")
        self.assertIn("__classificationEnabled", src)


class TestNestedConnTableFullWidth(unittest.TestCase):
    """The nested conn-table fills the row width like the deep-expand panel.

    Regression: `.conn-table { width: auto }` made an expanded *session*
    shrink to content, then jump to full width once a *connection* inside
    it was expanded. Pin it to full width (minus the indent) so the two
    expansion levels are visually consistent.
    """

    def setUp(self):
        self.fx = _ActivityFixture()

    def tearDown(self):
        self.fx.stop()

    def test_conn_table_uses_full_width(self):
        token = self.fx.login()
        status, _hdrs, body = _request(
            self.fx.port, "GET", "/__ui/style.css",
            headers={"Cookie": f"session={token}"},
        )
        self.assertEqual(status, 200)
        css = body.decode("utf-8")
        m = re.search(r"\.conn-table\s*\{[^}]*\}", css)
        self.assertIsNotNone(m, "no .conn-table rule found")
        rule = m.group(0)
        self.assertIn("width: calc(100% - 1.75rem)", rule)
        self.assertNotRegex(rule, r"width:\s*auto")


class TestStateUnicodeRendering(unittest.TestCase):
    """State column renders Unicode symbols, not colored dots."""

    def setUp(self):
        self.fx = _ActivityFixture()

    def tearDown(self):
        self.fx.stop()

    def test_session_table_uses_unicode_state_symbols(self):
        token = self.fx.login()
        status, _, body = _request(
            self.fx.port, "GET", "/__ui/session-table.js",
            headers={"Cookie": f"session={token}"},
        )
        self.assertEqual(status, 200)
        src = body.decode("utf-8")
        # Should reference unicode checkmark and cross symbols
        self.assertIn("✓", src)
        self.assertIn("✗", src)
        self.assertIn("●", src)
        # Should not create state-dot spans anymore
        self.assertNotIn("state-dot", src)


class TestConnTableColumnWidths(unittest.TestCase):

    def setUp(self):
        self.fx = _ActivityFixture()

    def tearDown(self):
        self.fx.stop()

    def test_fixed_columns_have_min_width(self):
        token = self.fx.login()
        status, _, body = _request(
            self.fx.port, "GET", "/__ui/style.css",
            headers={"Cookie": f"session={token}"},
        )
        self.assertEqual(status, 200)
        css = body.decode("utf-8")
        self.assertIn("min-width", css)
        # Verify column class selectors are present
        self.assertIn("conn-table-col-ts", css)
        self.assertIn("conn-table-col-duration", css)
        self.assertIn("conn-table-col-path", css)

    def test_header_th_uses_column_class_names(self):
        token = self.fx.login()
        status, _, body = _request(
            self.fx.port, "GET", "/__ui/session-table.js",
            headers={"Cookie": f"session={token}"},
        )
        self.assertEqual(status, 200)
        src = body.decode("utf-8")
        self.assertIn("conn-table-col-", src)


class TestSessionTimeSpentColumn(unittest.TestCase):

    def setUp(self):
        self.fx = _ActivityFixture()

    def tearDown(self):
        self.fx.stop()

    def test_session_table_has_time_spent_column(self):
        token = self.fx.login()
        status, _, body = _request(
            self.fx.port, "GET", "/__ui/session-table.js",
            headers={"Cookie": f"session={token}"},
        )
        self.assertEqual(status, 200)
        src = body.decode("utf-8")
        self.assertIn("time_spent", src)
        self.assertIn("time spent", src)


if __name__ == "__main__":
    unittest.main()
