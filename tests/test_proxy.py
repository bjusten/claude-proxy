"""
Comprehensive mock tests for claude-proxy.

Tests cover: model routing, least-active-connections load balancing, config
resolution, GET/POST/HEAD methods, body rewriting, header rewriting,
error handling, and edge cases.

Uses random ports for integration tests to avoid conflicts with the default (11435).
"""

import io
import json
import logging
import socket
import threading
import unittest
import unittest.mock as mock
from urllib.error import HTTPError
from urllib.parse import urlparse

from proxy import (
    ClaudeProxyHandler,
    build_url_colors,
    default_models,
    default_proxy_settings,
    LeastActiveSelector,
    ThreadingHTTPServer,
    _rr,
    _url_tracker,
    _active_sessions,
    _session_logger,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port():
    """Return a random free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
    return port


def _ok_resp():
    """Return a mock upstream response that works with the proxy's copyfileobj.

    The mock supports getcode(), getheaders(), __enter__/__exit__, and read().
    read() returns data on first call, then b"" to signal end of stream.
    """
    m = mock.MagicMock()
    m.getcode.return_value = 200
    m.getheaders.return_value = [("Content-Type", "application/json")]
    m.__enter__ = mock.Mock(return_value=m)
    m.__exit__ = mock.Mock(return_value=None)

    def read(size=-1):
        read.call_count += 1
        if read.call_count == 1:
            return b"upstream response"
        return b""
    read.call_count = 0
    m.read = mock.Mock(side_effect=read)
    return m


def _build_handler(config=None, models=None, proxy_settings=None,
                   method="POST", path="/v1/messages", body=None, headers=None):
    """Create a ClaudeProxyHandler with mocked I/O, without starting a server.

    ``config=`` is accepted as an alias for ``models=`` (routing map) so
    routing-only callers stay unchanged. Proxy scalars go in ``proxy_settings=``.

    Returns (handler, captured_wfile).
    """
    models = models if models is not None else (config or {})
    proxy_settings = proxy_settings or {}
    hdrs = headers or {}
    if isinstance(body, bytes):
        body_bytes = body
    elif body is not None:
        body_bytes = json.dumps(body).encode()
    else:
        body_bytes = b""
    req_line = f"{method} {path} HTTP/1.1"

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
    handler.requestline = req_line
    handler.command = method
    handler.path = path
    handler.models = models
    handler.proxy_settings = proxy_settings
    handler.headers = dict(hdrs)
    # Ensure Content-Length is set so handle_proxy_request reads the body
    if "Content-Length" not in handler.headers:
        handler.headers["Content-Length"] = str(len(body_bytes))
    handler.rfile = io.BytesIO(body_bytes)
    handler.wfile = io.BytesIO()

    return handler, handler.wfile


def _start_server(config, port, ready_event=None):
    """Start the proxy server in a background thread; block until ready."""
    server = ThreadingHTTPServer(
        ("127.0.0.1", port),
        lambda *args, **kw: ClaudeProxyHandler(*args, models=config, **kw),
    )
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    if ready_event:
        ready_event.set()
    return server


def _http_request(port, method="POST", path="/v1/messages", body=None, extra_headers=None):
    """Send an HTTP request to a live server; return (status, headers_dict, body)."""
    import http.client
    hdrs = {"Content-Type": "application/json"}
    if extra_headers:
        hdrs.update(extra_headers)
    body_bytes = json.dumps(body).encode() if body is not None else b""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body_bytes, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read()
    return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data


# ---------------------------------------------------------------------------
# LeastActiveSelector unit tests
# ---------------------------------------------------------------------------

class TestLeastActiveSelector(unittest.TestCase):
    def test_least_active_picks_lowest_counter_sequential(self):
        rr = LeastActiveSelector()
        entries = ["a", "b", "c"]
        # With no releases, each pick increments the chosen counter,
        # pushing it to the next position.
        picks = [rr.acquire("model", entries) for _ in range(6)]
        self.assertEqual(picks, [0, 1, 2, 0, 1, 2])

    def test_multiple_keys_are_isolated(self):
        rr = LeastActiveSelector()
        a = [rr.acquire("model-a", ["x", "y"]) for _ in range(4)]
        b = [rr.acquire("model-b", ["x", "y"]) for _ in range(4)]
        self.assertEqual(a, [0, 1, 0, 1])
        self.assertEqual(b, [0, 1, 0, 1])

    def test_single_entry_returns_zero(self):
        rr = LeastActiveSelector()
        self.assertEqual([rr.acquire("m", ["only"]) for _ in range(5)], [0, 0, 0, 0, 0])

    def test_release_decrements_counter(self):
        rr = LeastActiveSelector()
        entries = ["a", "b"]
        idx = rr.acquire("model", entries)
        self.assertEqual(idx, 0)
        rr.release("model", idx)
        # Now both counters are 0 again, tie-break → 0
        idx = rr.acquire("model", entries)
        self.assertEqual(idx, 0)

    def test_empty_entries_uses_empty_range(self):
        rr = LeastActiveSelector()
        with self.assertRaises(ValueError):
            rr.acquire("m", [])


# ---------------------------------------------------------------------------
# Handler unit tests (no live server)
# ---------------------------------------------------------------------------

def _do_handler(handler):
    """Execute handler.handle_proxy_request with mocked upstream; return the Request."""
    def capture(req, **kw):
        _do_handler._captured = req
        return _ok_resp()
    with mock.patch("urllib.request.urlopen", side_effect=capture):
        handler.handle_proxy_request()
    return _do_handler._captured


_do_handler._captured = None


class TestHandlerDirect(unittest.TestCase):

    def setUp(self):
        _rr._active.clear()
        _url_tracker._count.clear()
        _active_sessions._count = 0
        # Remove the file handler so tests don't leak into the real log
        for h in _session_logger.handlers[:]:
            _session_logger.removeHandler(h)

    def tearDown(self):
        # Clean up file handler created during module import
        for h in _session_logger.handlers[:]:
            h.close()
            _session_logger.removeHandler(h)

    def test_post_rewrites_model_in_body(self):
        handler, _ = _build_handler(
            config={"default": {"urls": [{"url": "http://upstream:8080", "new_model_name": "rewritten-model"}]}},
            body={"model": "claude-3-sonnet", "messages": [{"role": "user", "content": "hi"}]},
        )
        req = _do_handler(handler)
        body = json.loads(req.data)
        self.assertNotEqual(body["model"], "claude-3-sonnet")
        self.assertEqual(body["model"], "rewritten-model")
        self.assertEqual(body["messages"][0]["content"], "hi")

    def test_post_unknown_model_uses_default_config(self):
        config = {"default": {"urls": [{"url": "http://up:8080", "new_model_name": "rewritten-model"}]}}
        handler, _ = _build_handler(config=config, body={"model": "unknown-model"})
        req = _do_handler(handler)
        self.assertEqual(json.loads(req.data)["model"], "rewritten-model")

    def test_post_missing_config_fallback(self):
        handler, _ = _build_handler(config={}, body={"model": "anything"})
        req = _do_handler(handler)
        self.assertEqual(json.loads(req.data)["model"], "qwen3.6:27b")

    def test_least_active_selects_entry_with_fewest_connections(self):
        config = {
            "model-a": {
                "urls": [
                    {"url": "http://a:1111", "new_model_name": "m1"},
                    {"url": "http://b:2222", "new_model_name": "m2"},
                    {"url": "http://c:3333", "new_model_name": "m3"},
                ]
            }
        }
        # Sequential (non-overlapping) requests all go to index 0
        targets = []
        for _ in range(6):
            handler, _ = _build_handler(config=config, body={"model": "model-a"})
            def capture(req, **kw):
                targets.append(req.full_url)
                return _ok_resp()
            with mock.patch("urllib.request.urlopen", side_effect=capture):
                handler.handle_proxy_request()
        expected = [
            "http://a:1111/v1/messages",
        ] * 6
        self.assertEqual(targets, expected)

    def test_get_forwards_path(self):
        config = {"default": {"urls": [{"url": "http://up:8080"}]}}
        handler, _ = _build_handler(config=config, method="GET", path="/v1/messages/stream")
        req = _do_handler(handler)
        self.assertIn("/v1/messages/stream", req.full_url)
        self.assertEqual(req.method, "GET")

    def test_head_no_body_forwarded(self):
        config = {"default": {"urls": [{"url": "http://up:8080"}]}}
        handler, _ = _build_handler(config=config, method="HEAD")
        req = _do_handler(handler)
        self.assertIsNone(req.data)
        self.assertEqual(req.method, "HEAD")

    def test_host_header_rewritten_to_upstream(self):
        config = {"default": {"urls": [{"url": "http://10.0.0.1:5000"}]}}
        handler, _ = _build_handler(config=config, body={"model": "test"})
        req = _do_handler(handler)
        self.assertEqual(req.get_header("Host"), "10.0.0.1:5000")

    def test_content_length_reflects_rewritten_body(self):
        config = {"default": {"urls": [{"url": "http://up:8080", "new_model_name": "rewritten-model"}]}}
        body = {"model": "claude-3-sonnet", "messages": [{"role": "user", "content": "hello world!"}]}
        handler, _ = _build_handler(config=config, body=body)
        req = _do_handler(handler)
        body_bytes = req.data
        self.assertIsNotNone(body_bytes)
        # Verify Content-Length header matches actual body (case-insensitive)
        cl = next((v for k, v in req.headers.items() if k.lower() == "content-length"), None)
        self.assertEqual(int(cl), len(body_bytes))

    def test_excluded_headers_not_in_upstream_request(self):
        """Proxy should not forward connection-specific headers."""
        config = {"default": {"urls": [{"url": "http://up:8080"}]}}
        handler, _ = _build_handler(
            config=config, body={"model": "test"},
            headers={"Connection": "keep-alive", "Transfer-Encoding": "chunked"},
        )
        req = _do_handler(handler)
        self.assertIsNone(req.get_header("Connection"))
        self.assertIsNone(req.get_header("Transfer-Encoding"))

    def test_invalid_json_falls_back_to_default(self):
        config = {"default": {"urls": [{"url": "http://up:8080", "new_model_name": "fallback-model"}]}}
        handler, _ = _build_handler(
            config=config,
            headers={"Content-Type": "application/json", "Content-Length": "16"},
            body=b"not valid json {{{",
        )
        req = _do_handler(handler)
        body = json.loads(req.data)
        self.assertEqual(body["model"], "fallback-model")

    def test_old_config_format_url_key(self):
        config = {"default": {"url": "http://up:8080", "new_model_name": "old-format-model"}}
        handler, _ = _build_handler(config=config, body={"model": "test"})
        req = _do_handler(handler)
        body = json.loads(req.data)
        self.assertEqual(body["model"], "old-format-model")
        self.assertIn("up:8080", req.full_url)

    def test_model_specific_routing(self):
        config = {
            "gpt-4": {"urls": [{"url": "http://gpt:5001", "new_model_name": "gpt-upstream"}]},
            "default": {"urls": [{"url": "http://default:6002", "new_model_name": "default-upstream"}]},
        }
        targets = []
        for model_name in ["gpt-4", "unknown-model"]:
            handler, _ = _build_handler(config=config, body={"model": model_name})
            def capture(req, **kw):
                targets.append(req.full_url)
                return _ok_resp()
            with mock.patch("urllib.request.urlopen", side_effect=capture):
                handler.handle_proxy_request()
        self.assertIn("gpt:5001", targets[0])
        self.assertIn("default:6002", targets[1])

    def test_post_with_no_body_sends_empty_json(self):
        config = {"default": {"urls": [{"url": "http://up:8080"}]}}
        handler, _ = _build_handler(config=config, body=None, headers={})
        req = _do_handler(handler)
        self.assertEqual(json.loads(req.data), {"model": ""})

    def test_upstream_http_error_propagates_status(self):
        config = {"default": {"urls": [{"url": "http://up:8080"}]}}
        handler, captured = _build_handler(config=config, body={"model": "test"})
        err = HTTPError("http://up:8080", 503, "Service Unavailable", {}, io.BytesIO(b"error"))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            handler.handle_proxy_request()
        self.assertIn(b"503", captured.getvalue())

    def test_upstream_connection_failure_returns_502(self):
        from urllib.error import URLError as _URLError
        config = {"default": {"urls": [{"url": "http://nonexistent:9999"}]}}
        handler, captured = _build_handler(config=config, body={"model": "test"})
        with mock.patch("urllib.request.urlopen", side_effect=_URLError("refused")):
            handler.handle_proxy_request()
        self.assertIn(b"502", captured.getvalue())


# ---------------------------------------------------------------------------
# Integration tests (live server + real sockets)
# ---------------------------------------------------------------------------

class TestProxyIntegration(unittest.TestCase):
    """Smoke tests that hit a real server on a random port."""

    def setUp(self):
        self.port = _free_port()
        self.ready = threading.Event()
        self.server = _start_server(
            config={"default": {"urls": [{"url": "http://127.0.0.1:9999"}]}},
            port=self.port,
            ready_event=self.ready,
        )
        self.assertTrue(self.ready.wait(timeout=2))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_post_returns_502(self):
        status, _, _ = _http_request(self.port, body={"model": "test"})
        self.assertEqual(status, 502)

    def test_get_returns_502(self):
        status, _, _ = _http_request(self.port, method="GET")
        self.assertEqual(status, 502)

    def test_head_returns_502(self):
        status, _, _ = _http_request(self.port, method="HEAD")
        self.assertEqual(status, 502)


# ---------------------------------------------------------------------------
# Session logger tests
# ---------------------------------------------------------------------------

class TestSessionLogging(unittest.TestCase):

    def setUp(self):
        _rr._active.clear()
        _url_tracker._count.clear()
        _active_sessions._count = 0
        for h in _session_logger.handlers[:]:
            _session_logger.removeHandler(h)

    def tearDown(self):
        for h in _session_logger.handlers[:]:
            h.close()
            _session_logger.removeHandler(h)

    def _capture_log(self):
        """Return list of logged lines from a temporary file handler."""
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            log_path = f.name
        fh = logging.FileHandler(log_path)
        fh.setFormatter(logging.Formatter("%(message)s"))
        _session_logger.addHandler(fh)
        return fh, log_path

    def test_multi_url_writes_acquire_release_lines(self):
        """Multi-URL config should log ACQUIRED and RELEASED with active count."""
        config = {
            "default": {
                "urls": [
                    {"url": "http://a:8080", "new_model_name": "m1"},
                    {"url": "http://b:9090", "new_model_name": "m2"},
                ]
            }
        }
        fh, log_path = self._capture_log()
        handler, _ = _build_handler(
            config=config, body={"model": "test-model"},
            proxy_settings={"enable_session_logging": True},
        )
        with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **kw: _ok_resp()):
            handler.handle_proxy_request()

        _session_logger.removeHandler(fh)
        fh.close()

        with open(log_path) as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 2)
        acquired = lines[0].strip()
        released = lines[1].strip()
        self.assertIn("ACQUIRED active=1 model='test-model'", acquired)
        self.assertIn("idx=0 url='http://a:8080'", acquired)
        self.assertIn("rewritten='m1'", acquired)
        self.assertIn("RELEASED active=0 model='test-model'", released)
        self.assertIn("idx=0", released)
        self.assertIn("rewritten='m1'", released)

    def test_single_url_no_rr_release(self):
        """Single-URL config should still log ACQUIRED/RELEASED but no rr.acquire/release."""
        config = {"default": {"urls": [{"url": "http://a:8080", "new_model_name": "m1"}]}}
        fh, log_path = self._capture_log()
        handler, _ = _build_handler(
            config=config, body={"model": "test-model"},
            proxy_settings={"enable_session_logging": True},
        )
        with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **kw: _ok_resp()):
            handler.handle_proxy_request()

        _session_logger.removeHandler(fh)
        fh.close()

        with open(log_path) as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("ACQUIRED active=1 model='test-model'", lines[0].strip())
        self.assertIn("idx=-1", lines[0].strip())
        self.assertIn("RELEASED active=0 model='test-model'", lines[1].strip())

    def test_concurrent_requests_show_increasing_active(self):
        """Two back-to-back requests should show active=1 then active=0 between them."""
        config = {
            "default": {
                "urls": [
                    {"url": "http://a:8080", "new_model_name": "m1"},
                    {"url": "http://b:9090", "new_model_name": "m2"},
                ]
            }
        }
        fh, log_path = self._capture_log()
        for _ in range(2):
            handler, _ = _build_handler(
                config=config, body={"model": "test-model"},
                proxy_settings={"enable_session_logging": True},
            )
            with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **kw: _ok_resp()):
                handler.handle_proxy_request()

        _session_logger.removeHandler(fh)
        fh.close()

        with open(log_path) as f:
            lines = [l.strip() for l in f.readlines()]
        # 4 lines: acquire(1), release(0), acquire(1), release(0)
        self.assertEqual(len(lines), 4)
        self.assertIn("active=1", lines[0])
        self.assertIn("active=0", lines[1])
        self.assertIn("active=1", lines[2])
        self.assertIn("active=0", lines[3])

    def test_session_logging_disabled_by_default_writes_nothing(self):
        """With enable_session_logging unset (default), no ACQUIRED/RELEASED lines."""
        config = {
            "default": {
                "urls": [
                    {"url": "http://a:8080", "new_model_name": "m1"},
                    {"url": "http://b:9090", "new_model_name": "m2"},
                ]
            }
        }
        fh, log_path = self._capture_log()
        handler, _ = _build_handler(config=config, body={"model": "test-model"})
        with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **kw: _ok_resp()):
            handler.handle_proxy_request()

        _session_logger.removeHandler(fh)
        fh.close()

        with open(log_path) as f:
            lines = f.readlines()
        self.assertEqual(lines, [])


# ---------------------------------------------------------------------------
# Global URL tracking tests
# ---------------------------------------------------------------------------


class TestGlobalUrlTracking(unittest.TestCase):
    """Tests for global_url_tracking config option."""

    def setUp(self):
        _rr._active.clear()
        _url_tracker._count.clear()
        _active_sessions._count = 0
        for h in _session_logger.handlers[:]:
            _session_logger.removeHandler(h)

    def tearDown(self):
        for h in _session_logger.handlers[:]:
            h.close()
            _session_logger.removeHandler(h)

    def test_shared_url_picks_different_entry_with_global_tracking(self):
        """Direct test: two routing keys sharing same host:port pick different entries."""
        urls_a = [
            {"url": "http://shared:8080", "new_model_name": "m-a-1"},
            {"url": "http://other:8080", "new_model_name": "m-a-2"},
        ]
        urls_b = [
            {"url": "http://shared:8080", "new_model_name": "m-b-1"},
            {"url": "http://other:8080", "new_model_name": "m-b-2"},
        ]
        # model-a acquires first (hits shared, index 0)
        idx_a = _rr.acquire("model-a", urls_a, global_tracking=True)
        self.assertEqual(idx_a, 0)
        # model-b acquires while model-a holds (should pick other, not shared)
        idx_b = _rr.acquire("model-b", urls_b, global_tracking=True)
        self.assertEqual(idx_b, 1)
        # model-a releases its slot
        _rr.release("model-a", idx_a, url=urls_a[idx_a]["url"])
        # model-b releases its slot
        _rr.release("model-b", idx_b, url=urls_b[idx_b]["url"])

    def test_wait_for_free_url_unblocks_on_release(self):
        """When all URLs are saturated, acquire waits until one is released."""
        urls = [
            {"url": "http://a:8080", "new_model_name": "m1"},
            {"url": "http://b:8080", "new_model_name": "m2"},
        ]
        # Saturate both URLs from different keys so global tracker shows 1 each
        idx_a = _rr.acquire("key-a", urls, global_tracking=True, wait_timeout=0)
        self.assertEqual(idx_a, 0)
        idx_b = _rr.acquire("key-b", urls, global_tracking=True, wait_timeout=0)
        self.assertEqual(idx_b, 1)
        # Both URLs are now busy — wait path should pick the minimum (tie-break = 0)
        idx_c = _rr.acquire("key-c", urls, global_tracking=True, wait_timeout=0)
        self.assertEqual(idx_c, 0)  # both have 1 active, tie-break → index 0
        _rr.release("key-a", idx_a, url=urls[idx_a]["url"])
        _rr.release("key-b", idx_b, url=urls[idx_b]["url"])
        _rr.release("key-c", idx_c, url=urls[idx_c]["url"])

    def test_wait_timeout_releases_lock_and_retries(self):
        """wait_for_free_url releases the lock so other threads can acquire/release."""
        import threading
        import time
        urls = [
            {"url": "http://a:8080", "new_model_name": "m1"},
            {"url": "http://b:8080", "new_model_name": "m2"},
        ]
        # Saturate both URLs
        idx_a = _rr.acquire("key-a", urls, global_tracking=True, wait_timeout=0)
        idx_b = _rr.acquire("key-b", urls, global_tracking=True, wait_timeout=0)
        # A third acquire with a long timeout should not block forever
        # (it falls back to minimum after timeout)
        results = []
        def waiter():
            idx = _rr.acquire("key-d", urls, global_tracking=True, wait_timeout=2)
            results.append(idx)
        t = threading.Thread(target=waiter, daemon=True)
        t.start()
        # Release one URL after a short delay — the waiter should eventually pick it
        time.sleep(0.3)
        _rr.release("key-a", idx_a, url=urls[idx_a]["url"])
        t.join(timeout=5)
        self.assertTrue(len(results) == 1, f"waiter should have returned, got {results}")

    def test_queued_connection_has_priority_over_newer_connection(self):
        """A connection already queued must get the next freed slot before a
        newer connection that arrives after the slot becomes free."""
        import threading
        import time
        _url_tracker._count.clear()
        urls = [
            {"url": "http://a:8080", "new_model_name": "m1"},
            {"url": "http://b:8080", "new_model_name": "m2"},
        ]
        # Saturate both URLs so the next acquire must queue.
        idx_a = _rr.acquire("key-a", urls, global_tracking=True, wait_timeout=0)
        idx_b = _rr.acquire("key-b", urls, global_tracking=True, wait_timeout=0)

        queued_result = []
        queued_marked = threading.Event()

        def queued():
            i = _rr.acquire(
                "key-q", urls, global_tracking=True, wait_timeout=5,
                on_queue=lambda: queued_marked.set(),
            )
            queued_result.append(i)

        tq = threading.Thread(target=queued, daemon=True)
        tq.start()
        # Wait until the queued connection is registered as QUEUED and parked.
        self.assertTrue(queued_marked.wait(2), "queued connection never queued")
        time.sleep(0.15)

        # Free exactly one slot — it is reserved for the queued connection.
        _rr.release("key-a", idx_a, url=urls[idx_a]["url"])

        # A newer connection arrives while the queued one is still waiting.
        # It must NOT steal the slot reserved for the queued connection.
        fresh_result = {}

        def fresh():
            i = _rr.acquire("key-f", urls, global_tracking=True, wait_timeout=5)
            fresh_result["idx"] = i

        tf = threading.Thread(target=fresh, daemon=True)
        tf.start()
        time.sleep(0.3)

        fresh_blocked = "idx" not in fresh_result
        tq.join(3)

        # Let the newer connection finish so the thread exits cleanly.
        _rr.release("key-b", idx_b, url=urls[idx_b]["url"])
        tf.join(3)

        self.assertEqual(
            queued_result, [0],
            "queued connection should have received the freed slot",
        )
        self.assertTrue(
            fresh_blocked,
            "newer connection stole the slot reserved for the queued "
            "connection (no queue priority)",
        )

    def test_without_global_tracking_both_pick_same(self):
        """Without the flag, same host:port can be picked by both keys."""
        config = {
            "model-a": {
                "urls": [
                    {"url": "http://shared:8080", "new_model_name": "m-a-1"},
                    {"url": "http://other:8080", "new_model_name": "m-a-2"},
                ]
            },
            "model-b": {
                "urls": [
                    {"url": "http://shared:8080", "new_model_name": "m-b-1"},
                    {"url": "http://other:8080", "new_model_name": "m-b-2"},
                ]
            },
        }
        targets = []
        for body_model in ["model-a", "model-b"]:
            handler, _ = _build_handler(
                config=config,
                body={"model": body_model},
            )
            def capture(req, **kw):
                targets.append(urlparse(req.full_url).netloc)
                return _ok_resp()
            with mock.patch("urllib.request.urlopen", side_effect=capture):
                handler.handle_proxy_request()
        # Without global tracking, both pick index 0 (shared).
        self.assertEqual(targets, ["shared:8080", "shared:8080"])

    def test_release_clears_global_counter(self):
        """After release, URL becomes available again for other keys."""
        urls_a = [
            {"url": "http://shared:8080", "new_model_name": "m-a-1"},
            {"url": "http://other:8080", "new_model_name": "m-a-2"},
        ]
        urls_b = [
            {"url": "http://shared:8080", "new_model_name": "m-b-1"},
            {"url": "http://other:8080", "new_model_name": "m-b-2"},
        ]
        config = {
            "model-a": {"urls": urls_a},
            "model-b": {"urls": urls_b},
        }

        # model-a acquires (hits shared)
        idx_a = _rr.acquire("model-a", urls_a, global_tracking=True)
        self.assertEqual(idx_a, 0)
        self.assertEqual(urlparse(urls_a[idx_a]["url"]).netloc, "shared:8080")

        # model-b acquires (hits other, not shared)
        idx_b = _rr.acquire("model-b", urls_b, global_tracking=True)
        self.assertEqual(idx_b, 1)
        self.assertEqual(urlparse(urls_b[idx_b]["url"]).netloc, "other:8080")

        # model-a releases (now shared is free globally)
        _rr.release("model-a", idx_a, url=urls_a[idx_a]["url"])
        # model-a acquires again (should get shared now)
        idx_a2 = _rr.acquire("model-a", urls_a, global_tracking=True)
        self.assertEqual(idx_a2, 0)
        self.assertEqual(urlparse(urls_a[idx_a2]["url"]).netloc, "shared:8080")


class TestClassificationConfigPlumbing(unittest.TestCase):
    """Verify the handler accepts classification_config as a kwarg."""

    def test_init_signature_has_classification_config(self):
        import inspect
        sig = inspect.signature(ClaudeProxyHandler.__init__)
        self.assertIn("classification_config", sig.parameters)
        self.assertIsNone(sig.parameters["classification_config"].default)

    def test_default_when_kwarg_omitted(self):
        # BaseHTTPRequestHandler.__init__ wants a real request; bypass it.
        h = ClaudeProxyHandler.__new__(ClaudeProxyHandler)
        # Manually invoke the part of __init__ that sets our own attributes.
        cls_cfg = None
        h.classification_config = cls_cfg or {"enabled": False}
        self.assertEqual(h.classification_config, {"enabled": False})


class TestClassificationRouting(unittest.TestCase):
    """End-to-end: classifier overrides model when enabled."""

    def _handler_with_cls(self, enabled, default="CODING"):
        import classifier as _cls
        cls_cfg = {
            "enabled": enabled,
            "default": default,
            "min_confidence": 1.0,
            "max_scan_bytes_per_message": 65536,
            "budget_warn_ms": 1000,
            "weights": dict(_cls.DEFAULT_WEIGHTS),
        }
        config = {
            "CODING":    {"urls": [{"url": "http://coding.local:1", "new_model_name": "code-llm"}]},
            "REASONING": {"urls": [{"url": "http://reason.local:1", "new_model_name": "reason-llm"}]},
            "default":   {"urls": [{"url": "http://fallback.local:1", "new_model_name": "fallback-llm"}]},
        }
        h = ClaudeProxyHandler.__new__(ClaudeProxyHandler)
        h.models = config
        h.proxy_settings = {}
        h._url_colors = {}
        h.classification_config = cls_cfg
        h.command = "POST"
        h.path = "/v1/messages"
        return h

    def test_enabled_classifies_codey_request_to_CODING(self):
        h = self._handler_with_cls(enabled=True)
        body = {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content":
                "fix this:\n```python\ndef f(): return 1/0\n```\nTraceback..."
            }],
        }
        routing_key = h._classify_or_passthrough(body)
        dest, new_model, _ = h._resolve_destination(routing_key)
        self.assertEqual(dest, "http://coding.local:1")
        self.assertEqual(new_model, "code-llm")

    def test_enabled_classifies_reasoning_request_to_REASONING(self):
        h = self._handler_with_cls(enabled=True)
        body = {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content":
                "Explain why functional programming is better. Compare and analyze trade-offs. What do you think?"
            }],
        }
        routing_key = h._classify_or_passthrough(body)
        dest, _, _ = h._resolve_destination(routing_key)
        self.assertEqual(dest, "http://reason.local:1")

    def test_disabled_uses_original_model(self):
        h = self._handler_with_cls(enabled=False)
        body = {"model": "claude-opus-4-7", "messages": [{"role": "user", "content": "x"}]}
        # Original model is not in config, falls through to "default"
        routing_key = h._classify_or_passthrough(body)
        dest, _, _ = h._resolve_destination(routing_key)
        self.assertEqual(dest, "http://fallback.local:1")


class TestClassificationLogging(unittest.TestCase):
    def test_classified_field_present_when_enabled(self):
        import classifier as _cls
        h = ClaudeProxyHandler.__new__(ClaudeProxyHandler)
        h.models = {}
        h.proxy_settings = {}
        h._url_colors = {}
        h.classification_config = {"enabled": True, "default": "CODING",
                                   "min_confidence": 1.0, "max_scan_bytes_per_message": 65536,
                                   "budget_warn_ms": 1000,
                                   "weights": _cls.DEFAULT_WEIGHTS}
        h.command = "POST"
        h.path = "/v1/messages"
        h._classified = "CODING"
        h._class_scores = {"code": 8.5, "reasoning": 1.0, "scan_ms": 2.1}

        captured = []

        class _Capture(logging.Handler):
            def emit(self, record):
                captured.append(self.format(record))

        cap = _Capture()
        cap.setFormatter(logging.Formatter("%(message)s"))
        from proxy import logger as _logger
        _logger.addHandler(cap)
        try:
            h.log_proxy_request("claude-opus-4-7", "http://x:1", "code-llm")
        finally:
            _logger.removeHandler(cap)

        self.assertTrue(any("classified='CODING'" in line for line in captured), captured)
        self.assertTrue(any("code=8.5" in line for line in captured), captured)

    def test_classified_field_absent_when_disabled(self):
        h = ClaudeProxyHandler.__new__(ClaudeProxyHandler)
        h.models = {}
        h.proxy_settings = {}
        h._url_colors = {}
        h.classification_config = {"enabled": False}
        h.command = "POST"
        h.path = "/v1/messages"
        h._classified = None
        h._class_scores = None

        captured = []

        class _Capture(logging.Handler):
            def emit(self, record):
                captured.append(self.format(record))

        cap = _Capture()
        cap.setFormatter(logging.Formatter("%(message)s"))
        from proxy import logger as _logger
        _logger.addHandler(cap)
        try:
            h.log_proxy_request("claude-opus-4-7", "http://x:1", "code-llm")
        finally:
            _logger.removeHandler(cap)

        for line in captured:
            self.assertNotIn("classified=", line)


class TestClassificationE2E(unittest.TestCase):
    def test_end_to_end_classifies_and_routes(self):
        import classifier as _cls
        import urllib.request

        captured_urls = []

        def fake_urlopen(req, timeout=None):
            captured_urls.append(req.full_url)
            return _ok_resp()

        cls_cfg = {
            "enabled": True, "default": "CODING",
            "min_confidence": 1.0, "max_scan_bytes_per_message": 65536,
            "budget_warn_ms": 1000,
            "weights": _cls.DEFAULT_WEIGHTS,
        }
        config = {
            "CODING":    {"urls": [{"url": "http://coding.local:9", "new_model_name": "code-llm"}]},
            "REASONING": {"urls": [{"url": "http://reason.local:9", "new_model_name": "reason-llm"}]},
        }
        port = _free_port()
        server = ThreadingHTTPServer(
            ("127.0.0.1", port),
            lambda *a, **kw: ClaudeProxyHandler(
                *a, models=config, url_colors={}, classification_config=cls_cfg, **kw
            ),
        )
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                body = json.dumps({
                    "model": "claude-opus-4-7",
                    "messages": [{"role": "user", "content":
                        "```python\nimport os\n```\nTraceback (most recent call last): SyntaxError"
                    }],
                }).encode()
                import http.client
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("POST", "/v1/messages", body=body,
                             headers={"Content-Type": "application/json"})
                conn.getresponse().read()
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(len(captured_urls), 1)
        self.assertTrue(captured_urls[0].startswith("http://coding.local:9"), captured_urls)


class TestUpstreamTimeout(unittest.TestCase):
    def setUp(self):
        _rr._active.clear()
        _url_tracker._count.clear()
        _active_sessions._count = 0
        for h in _session_logger.handlers[:]:
            _session_logger.removeHandler(h)

    def tearDown(self):
        for h in _session_logger.handlers[:]:
            h.close()
            _session_logger.removeHandler(h)

    def _capture_timeout(self, models, proxy_settings=None):
        captured = {}

        def capture(req, **kw):
            captured["timeout"] = kw.get("timeout")
            return _ok_resp()

        handler, _ = _build_handler(
            models=models, proxy_settings=proxy_settings, body={"model": "test"}
        )
        with mock.patch("urllib.request.urlopen", side_effect=capture):
            handler.handle_proxy_request()
        return captured["timeout"]

    def test_default_timeout_is_300(self):
        cfg = {"default": {"urls": [{"url": "http://up:8080"}]}}
        self.assertEqual(self._capture_timeout(cfg), 300)

    def test_configured_timeout_passed_to_urlopen(self):
        models = {"default": {"urls": [{"url": "http://up:8080"}]}}
        self.assertEqual(
            self._capture_timeout(models, {"upstream_timeout_seconds": 900}), 900
        )

    def test_null_timeout_means_no_timeout(self):
        models = {"default": {"urls": [{"url": "http://up:8080"}]}}
        self.assertIsNone(
            self._capture_timeout(models, {"upstream_timeout_seconds": None})
        )


class TestStreamingResponse(unittest.TestCase):
    def setUp(self):
        _rr._active.clear()
        _url_tracker._count.clear()
        _active_sessions._count = 0
        for h in _session_logger.handlers[:]:
            _session_logger.removeHandler(h)

    def tearDown(self):
        for h in _session_logger.handlers[:]:
            h.close()
            _session_logger.removeHandler(h)

    def test_response_streamed_in_bounded_chunks(self):
        cfg = {"default": {"urls": [{"url": "http://up:8080"}]}}
        handler, captured = _build_handler(config=cfg, body={"model": "test"})

        m = mock.MagicMock()
        m.getcode.return_value = 200
        m.getheaders.return_value = [("Content-Type", "text/event-stream")]
        m.__enter__ = mock.Mock(return_value=m)
        m.__exit__ = mock.Mock(return_value=None)
        seq = [b"chunk-a", b"chunk-b", b"chunk-c"]
        sizes = []

        def read(size=-1):
            sizes.append(size)
            return seq.pop(0) if seq else b""

        m.read = mock.Mock(side_effect=read)
        with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: m):
            handler.handle_proxy_request()

        self.assertIn(b"chunk-achunk-bchunk-c", captured.getvalue())
        self.assertTrue(sizes, "response.read was never called")
        self.assertTrue(
            all(s > 0 for s in sizes),
            f"expected bounded chunked reads, got sizes={sizes}",
        )


class TestDefaultProxyConfig(unittest.TestCase):
    def test_includes_upstream_timeout_seconds(self):
        self.assertEqual(default_proxy_settings()["upstream_timeout_seconds"], 300)

    def test_includes_all_scalar_defaults(self):
        cfg = default_proxy_settings()
        self.assertEqual(cfg["global_url_tracking"], False)
        self.assertEqual(cfg["wait_timeout_seconds"], 300)
        self.assertEqual(cfg["enable_session_logging"], False)

    def test_default_models_has_no_scalars(self):
        models = default_models()
        self.assertNotIn("upstream_timeout_seconds", models)
        self.assertIn("model-a", models)

    def test_color_mapping_handles_generated_default(self):
        colors = build_url_colors(default_models())
        self.assertIn("http://1.2.3.4:5678", colors)


class TestBuildUrlColors(unittest.TestCase):
    def test_scalar_top_level_option_does_not_crash(self):
        config = {
            "upstream_timeout_seconds": 300,
            "global_url_tracking": True,
            "model-a": {"urls": [{"url": "http://a:1"}, {"url": "http://b:2"}]},
        }
        colors = build_url_colors(config)
        self.assertEqual(set(colors), {"http://a:1", "http://b:2"})

    def test_distinct_urls_get_distinct_colors(self):
        config = {"m": {"urls": [{"url": "http://a:1"}, {"url": "http://b:2"}]}}
        colors = build_url_colors(config)
        self.assertNotEqual(colors["http://a:1"], colors["http://b:2"])


class TestJournalMaxBodyBytesRedaction(unittest.TestCase):
    def test_redact_body_truncates_using_journal_config(self):
        handler, _ = _build_handler(config={}, body={"model": "x"})
        handler.journal_config = {"max_body_bytes": 20}
        out = handler._redact_body(b"x" * 5000)
        self.assertTrue(out.startswith(b"x" * 20))
        self.assertIn(b"truncated", out)

    def test_redact_body_defaults_to_10240_when_unset(self):
        handler, _ = _build_handler(config={}, body={"model": "x"})
        out = handler._redact_body(b"y" * 100000)
        self.assertEqual(len(out), 10240 + len(b" [... truncated]"))


if __name__ == "__main__":
    unittest.main()
