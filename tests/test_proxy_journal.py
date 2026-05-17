"""Tests for journal integration into ClaudeProxyHandler.

Covers:
  - With journal_store set, /__journal/* short-circuits BEFORE upstream routing
  - With journal_store=None, /__journal/* returns 404 (only /v1/* is proxied)
  - End-to-end live-server smoke (mirrors TestProxyIntegration)
  - Lifecycle state transitions written to proxy_connection journal entries
"""

import io
import json
import socket
import threading
import unittest
import unittest.mock as mock
from urllib.error import URLError

from proxy import ClaudeProxyHandler, ThreadingHTTPServer
from journal.store import JournalStore


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        return s.getsockname()[1]


def _build_handler(*, config, journal_store, method="GET", path="/__journal/entries", body=None):
    """Build a ClaudeProxyHandler with mocked I/O, no server."""
    body_bytes = json.dumps(body).encode() if body is not None else b""
    orig_handle = mock.patch.object(ClaudeProxyHandler.__bases__[0], "handle", lambda self: None)
    orig_handle.start()
    try:
        handler = ClaudeProxyHandler(
            request=mock.MagicMock(),
            client_address=("127.0.0.1", 12345),
            server=mock.MagicMock(),
        )
    finally:
        orig_handle.stop()

    handler.request_version = "HTTP/1.1"
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.command = method
    handler.path = path
    handler.models = config
    handler.proxy_settings = {}
    handler.headers = {"Content-Length": str(len(body_bytes))}
    handler.rfile = io.BytesIO(body_bytes)
    handler.wfile = io.BytesIO()
    handler.journal_store = journal_store
    return handler


def _make_store_with_proxy_launched():
    store = JournalStore()
    store.append(
        {"id": "abc", "type": "proxy_launched", "ts": "2026-05-14T00:00:00Z", "pid": 12345, "port": 11435},
        pinned=True,
    )
    return store


class TestHandlerJournalShortCircuit(unittest.TestCase):
    def test_journal_path_short_circuits_when_store_set(self):
        store = _make_store_with_proxy_launched()
        handler = _build_handler(
            config={"default": {"urls": [{"url": "http://up:8080"}]}},
            journal_store=store,
        )
        # urlopen must NOT be called — if the short-circuit fails and the proxy
        # tries upstream, this raise will surface.
        with mock.patch("urllib.request.urlopen", side_effect=URLError("upstream should not be called")):
            handler.do_GET()
        written = handler.wfile.getvalue()
        self.assertIn(b"200", written.split(b"\r\n", 1)[0])
        # Find body (after blank line) and parse JSON
        _, _, body = written.partition(b"\r\n\r\n")
        decoded = json.loads(body)
        self.assertEqual(len(decoded), 1)
        self.assertEqual(decoded[0]["id"], "abc")


class TestHandlerJournalFallthrough(unittest.TestCase):
    def test_journal_path_rejected_when_store_none(self):
        handler = _build_handler(
            config={"default": {"urls": [{"url": "http://up:8080"}]}},
            journal_store=None,
        )
        # urlopen must NOT be called — non-/v1/* paths are 404'd, not proxied.
        with mock.patch("urllib.request.urlopen", side_effect=URLError("upstream should not be called")):
            handler.do_GET()
        written = handler.wfile.getvalue()
        self.assertIn(b"404", written.split(b"\r\n", 1)[0])


class TestProxyJournalE2E(unittest.TestCase):
    """Live server: GET /__journal/entries surfaces the proxy_launched entry."""

    def setUp(self):
        from proxy import make_proxy_launched_entry

        self.port = _free_port()
        self.store = JournalStore()
        entry = make_proxy_launched_entry(self.port)
        self.expected_pid = entry["pid"]
        self.store.append(entry, pinned=True)

        config = {"default": {"urls": [{"url": "http://127.0.0.1:9999"}]}}
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", self.port),
            lambda *args, **kw: ClaudeProxyHandler(*args, models=config, journal_store=self.store, **kw),
        )
        self.t = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _get(self, path):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.read()

    def test_entries_lists_proxy_launched_with_required_fields(self):
        status, body = self._get("/__journal/entries")
        self.assertEqual(status, 200)
        entries = json.loads(body)
        self.assertEqual(len(entries), 1)
        e = entries[0]
        for field in ("id", "type", "ts", "pid", "port"):
            self.assertIn(field, e)
        self.assertEqual(e["type"], "proxy_launched")
        self.assertEqual(e["port"], self.port)
        self.assertEqual(e["pid"], self.expected_pid)

    def test_entries_by_id_returns_full_entry(self):
        _, list_body = self._get("/__journal/entries")
        entry_id = json.loads(list_body)[0]["id"]
        status, body = self._get(f"/__journal/entries/{entry_id}")
        self.assertEqual(status, 200)
        deep = json.loads(body)
        self.assertEqual(deep["id"], entry_id)

    def test_unknown_id_returns_404(self):
        status, body = self._get("/__journal/entries/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body))


def _ok_upstream(body=b'{"ok":true}', status=200, headers=None):
    """Build a mock urlopen context-manager response."""
    headers = headers or [("Content-Type", "application/json")]
    m = mock.MagicMock()
    m.getcode.return_value = status
    m.getheaders.return_value = headers
    m.__enter__ = mock.Mock(return_value=m)
    m.__exit__ = mock.Mock(return_value=None)
    chunks = [body, b""]
    idx = {"i": 0}

    def read(size=-1):
        i = idx["i"]
        idx["i"] += 1
        return chunks[i] if i < len(chunks) else b""

    m.read = mock.Mock(side_effect=read)
    return m


def _build_post_handler(*, config, journal_store, body=None):
    """Build a ClaudeProxyHandler for a POST /v1/messages request."""
    body_bytes = json.dumps(body or {"model": "default-model", "messages": []}).encode()
    orig_handle = mock.patch.object(ClaudeProxyHandler.__bases__[0], "handle", lambda self: None)
    orig_handle.start()
    try:
        handler = ClaudeProxyHandler(
            request=mock.MagicMock(),
            client_address=("127.0.0.1", 12345),
            server=mock.MagicMock(),
        )
    finally:
        orig_handle.stop()

    handler.request_version = "HTTP/1.1"
    handler.requestline = "POST /v1/messages HTTP/1.1"
    handler.command = "POST"
    handler.path = "/v1/messages"
    handler.models = config
    handler.proxy_settings = {}
    handler.headers = {"Content-Length": str(len(body_bytes))}
    handler.rfile = io.BytesIO(body_bytes)
    handler.wfile = io.BytesIO()
    handler.journal_store = journal_store
    handler.classification_config = {"enabled": False}
    return handler


class TestStateTransitions(unittest.TestCase):
    """Verify lifecycle state constants are written at each transition."""

    _CONFIG = {"default": {"url": "http://up"}}

    def _collect_states(self, store):
        """Return list of (entry_id, state) from update calls on the store."""
        states = []
        original_update = store.update

        def tracking_update(entry_id, patch):
            original_update(entry_id, patch)
            if "state" in patch:
                states.append(patch["state"])

        store.update = tracking_update
        return states

    def test_state_transitions_success_path(self):
        store = JournalStore()
        handler = _build_post_handler(config=self._CONFIG, journal_store=store)
        states = self._collect_states(store)

        with mock.patch("urllib.request.urlopen", return_value=_ok_upstream(status=200)):
            handler.do_POST()

        # Both routing phases must appear, in order (send → await response)
        self.assertIn("ROUTING_REQUEST", states)
        self.assertIn("ROUTING_RESPONSE", states)
        self.assertLess(states.index("ROUTING_REQUEST"),
                        states.index("ROUTING_RESPONSE"))
        # Final deep entry state must be SUCCESS
        shallow_entries = store.list_shallow()
        proxy_entries = [e for e in shallow_entries if e.get("type") == "proxy_connection"]
        self.assertEqual(len(proxy_entries), 1)
        entry_id = proxy_entries[0]["id"]
        deep = store.get_deep(entry_id)
        self.assertEqual(deep.get("state"), "SUCCESS")

    def test_state_classifying_set_before_routing_when_classification_enabled(self):
        store = JournalStore()
        handler = _build_post_handler(config=self._CONFIG, journal_store=store)
        handler.classification_config = {"enabled": True}
        states = self._collect_states(store)

        with mock.patch("classifier.classify_request",
                        return_value=("default",
                                      {"scan_ms": 1.0, "code": 0.0,
                                       "reasoning": 0.0})), \
             mock.patch("urllib.request.urlopen",
                        return_value=_ok_upstream(status=200)):
            handler.do_POST()

        self.assertIn("CLASSIFYING", states)
        self.assertIn("ROUTING_REQUEST", states)
        self.assertLess(states.index("CLASSIFYING"),
                        states.index("ROUTING_REQUEST"))
        proxy_entries = [e for e in store.list_shallow()
                         if e.get("type") == "proxy_connection"]
        deep = store.get_deep(proxy_entries[0]["id"])
        self.assertEqual(deep.get("state"), "SUCCESS")

    def test_state_failure_on_4xx(self):
        store = JournalStore()
        handler = _build_post_handler(config=self._CONFIG, journal_store=store)

        with mock.patch("urllib.request.urlopen", return_value=_ok_upstream(status=404)):
            handler.do_POST()

        shallow_entries = store.list_shallow()
        proxy_entries = [e for e in shallow_entries if e.get("type") == "proxy_connection"]
        self.assertEqual(len(proxy_entries), 1)
        entry_id = proxy_entries[0]["id"]
        deep = store.get_deep(entry_id)
        self.assertEqual(deep.get("state"), "FAILURE")

    def test_state_failure_on_urlerror(self):
        store = JournalStore()
        handler = _build_post_handler(config=self._CONFIG, journal_store=store)

        with mock.patch("urllib.request.urlopen", side_effect=URLError("boom")):
            handler.do_POST()

        shallow_entries = store.list_shallow()
        proxy_entries = [e for e in shallow_entries if e.get("type") == "proxy_connection"]
        self.assertEqual(len(proxy_entries), 1)
        entry_id = proxy_entries[0]["id"]
        deep = store.get_deep(entry_id)
        self.assertEqual(deep.get("state"), "FAILURE")

    def test_acquire_global_invokes_on_queue_once_when_blocked(self):
        from proxy import LeastActiveSelector, _url_tracker
        sel = LeastActiveSelector()
        entries = [{"url": "http://a"}, {"url": "http://b"}]
        _url_tracker.acquire("http://a")
        _url_tracker.acquire("http://b")
        calls = []

        def freer():
            import time
            time.sleep(0.05)
            _url_tracker.release("http://a")

        threading.Thread(target=freer, daemon=True).start()
        idx = sel.acquire("k", entries, global_tracking=True, wait_timeout=5, on_queue=lambda: calls.append(1))
        self.assertEqual(len(calls), 1)
        self.assertIn(idx, (0, 1))
        _url_tracker.release(entries[idx]["url"])


if __name__ == "__main__":
    unittest.main()
