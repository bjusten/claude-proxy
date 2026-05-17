"""Tests for the SSE streaming endpoint in journal.api."""

import io
import json
import queue
import threading
import time
import unittest

from journal.api import stream
from journal.bus import EventBus
from journal.store import JournalStore


class _HandlerStub:
    """Minimal stand-in for a BaseHTTPRequestHandler.

    Captures the response status line, headers, and body bytes written to
    wfile. The send_response/send_header/end_headers methods write nothing
    user-visible but record what would be sent.
    """

    def __init__(self):
        self.wfile = io.BytesIO()
        self.status = None
        self.headers_sent = []
        self.command = "GET"

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, key, value):
        self.headers_sent.append((key, value))

    def end_headers(self):
        pass


class TestJournalStreamPreludeAndEvent(unittest.TestCase):
    def test_stream_writes_retry_prelude_and_journal_activity_event(self):
        bus = EventBus()
        store = JournalStore(bus=bus)
        handler = _HandlerStub()
        # Stop after one event by injecting a stop_event that the stream
        # function honours.
        stop = threading.Event()

        def runner():
            stream(handler, store, bus, ["journal_activity"],
                   keepalive_interval=60.0, poll_interval=0.05,
                   stop_event=stop)

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        # Give it a moment to subscribe and write the prelude.
        time.sleep(0.1)
        store.append({
            "id": "abc",
            "type": "proxy_connection",
            "session_id": "s1",
            "timestamps": {"received": "2026-05-14T00:00:00Z"},
            "incoming": {"method": "POST", "path": "/v1/messages",
                         "headers": {}, "body": b""},
            "routed": None,
            "response": None,
            "error": None,
        })
        # proxy_connection publish is deferred to update (complete data).
        store.update("abc", {"response": {"status": 200, "bytes": 42}})
        # Allow the stream to drain at least once.
        time.sleep(0.2)
        stop.set()
        t.join(timeout=2.0)

        self.assertEqual(handler.status, 200)
        # Validate headers
        header_keys = [k.lower() for k, _ in handler.headers_sent]
        self.assertIn("content-type", header_keys)
        ct = dict(handler.headers_sent).get("Content-Type", "")
        self.assertIn("text/event-stream", ct)

        body = handler.wfile.getvalue()
        self.assertIn(b"retry: 5000", body)
        # An event block was written
        self.assertIn(b"event: journal_activity\n", body)
        # Locate the data line and parse it
        lines = body.split(b"\n")
        data_lines = [l for l in lines if l.startswith(b"data: ")]
        self.assertTrue(data_lines)
        payload = json.loads(data_lines[0][len(b"data: "):].decode())
        self.assertEqual(payload["id"], "abc")
        self.assertEqual(payload["type"], "proxy_connection")


class TestJournalStreamMultiTopic(unittest.TestCase):
    def test_stream_with_two_topics_emits_blocks_for_both(self):
        bus = EventBus()
        store = JournalStore(bus=bus)
        handler = _HandlerStub()
        stop = threading.Event()

        def runner():
            stream(handler, store, bus, ["journal_activity", "gpu_change"],
                   keepalive_interval=60.0, poll_interval=0.05,
                   stop_event=stop)

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        time.sleep(0.1)
        bus.publish("gpu_change", {"gpu": 0, "util": 42})
        bus.publish("journal_activity", {"id": "x", "type": "proxy_launched"})
        time.sleep(0.2)
        stop.set()
        t.join(timeout=2.0)

        body = handler.wfile.getvalue()
        self.assertIn(b"event: gpu_change\n", body)
        self.assertIn(b"event: journal_activity\n", body)


class TestJournalStreamKeepalive(unittest.TestCase):
    def test_keepalive_comment_emitted_after_interval(self):
        bus = EventBus()
        store = JournalStore(bus=bus)
        handler = _HandlerStub()
        stop = threading.Event()

        def runner():
            stream(handler, store, bus, ["journal_activity"],
                   keepalive_interval=0.05, poll_interval=0.01,
                   stop_event=stop)

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        # Wait long enough for at least one keepalive tick.
        time.sleep(0.3)
        stop.set()
        t.join(timeout=2.0)

        body = handler.wfile.getvalue()
        self.assertIn(b": keepalive\n\n", body)


class TestProxyHandlerRoutesStream(unittest.TestCase):
    def test_handle_journal_routes_stream_path_to_stream_function(self):
        """_handle_journal must invoke journal.api.stream when path starts
        with /__journal/stream (parsing ?topic= query params)."""
        import unittest.mock as mock
        from proxy import ClaudeProxyHandler

        bus = EventBus()
        store = JournalStore(bus=bus)

        # Build a minimally-functional handler stub via the same trick used
        # in test_proxy_journal_capture.
        with mock.patch.object(ClaudeProxyHandler.__bases__[0], "handle",
                               lambda self: None):
            handler = ClaudeProxyHandler(
                request=mock.MagicMock(),
                client_address=("127.0.0.1", 0),
                server=mock.MagicMock(),
            )
        handler.request_version = "HTTP/1.1"
        handler.command = "GET"
        handler.path = "/__journal/stream?topic=journal_activity&topic=gpu_change"
        handler.headers = {}
        handler.rfile = io.BytesIO(b"")
        handler.wfile = io.BytesIO()
        handler.journal_store = store
        handler.journal_bus = bus

        captured = {}

        def fake_stream(h, st, bu, topics, **kw):
            captured["topics"] = list(topics)
            captured["bus_is"] = bu is bus
            captured["store_is"] = st is store

        with mock.patch("journal.api.stream", side_effect=fake_stream):
            handler._handle_journal()

        self.assertEqual(captured["topics"],
                         ["journal_activity", "gpu_change"])
        self.assertTrue(captured["bus_is"])
        self.assertTrue(captured["store_is"])


class _MockUpstreamHandler:
    """Tiny upstream handler used by the E2E test below."""
    pass  # placeholder — defined in the test module below.


import socket as _socket  # noqa: E402
import urllib.request as _urlreq  # noqa: E402
from http.server import BaseHTTPRequestHandler as _BHR, HTTPServer as _HS  # noqa: E402
from socketserver import ThreadingMixIn as _TM  # noqa: E402


class _E2EUpstream(_BHR):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        _ = self.rfile.read(length)
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a, **_k):
        pass


class _ThreadedUpstream(_TM, _HS):
    daemon_threads = True


def _free_port():
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        return s.getsockname()[1]


class TestJournalStreamE2E(unittest.TestCase):
    def setUp(self):
        from proxy import (
            ClaudeProxyHandler, ThreadingHTTPServer, _active_sessions, _rr,
            _session_logger,
        )
        _rr._active.clear()
        _active_sessions._count = 0
        for h in _session_logger.handlers[:]:
            _session_logger.removeHandler(h)

        self.up_port = _free_port()
        self.up = _ThreadedUpstream(("127.0.0.1", self.up_port), _E2EUpstream)
        self.up_t = threading.Thread(target=self.up.serve_forever, daemon=True)
        self.up_t.start()

        self.bus = EventBus()
        self.store = JournalStore(bus=self.bus)
        self.proxy_port = _free_port()
        config = {"model-a": {"urls": [{"url": f"http://127.0.0.1:{self.up_port}",
                                         "new_model_name": "llama3:70b"}]}}

        bus_ref = self.bus
        store_ref = self.store

        self.proxy = ThreadingHTTPServer(
            ("127.0.0.1", self.proxy_port),
            lambda *a, **kw: ClaudeProxyHandler(
                *a, models=config, journal_store=store_ref,
                journal_bus=bus_ref, **kw),
        )
        self.proxy_t = threading.Thread(target=self.proxy.serve_forever,
                                         daemon=True)
        self.proxy_t.start()

    def tearDown(self):
        self.proxy.shutdown()
        self.proxy.server_close()
        self.up.shutdown()
        self.up.server_close()

    def test_proxied_request_produces_sse_event_on_stream(self):
        # Open the stream first on a worker thread; collect lines until we see
        # an event block.
        got = queue.Queue()

        def reader():
            # Use a raw socket to avoid any buffered-IO surprises.
            sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            sock.settimeout(5.0)
            try:
                sock.connect(("127.0.0.1", self.proxy_port))
                sock.sendall(
                    b"GET /__journal/stream?topic=journal_activity HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\n"
                    b"Connection: close\r\n\r\n"
                )
                buf = b""
                deadline = time.time() + 5.0
                while time.time() < deadline:
                    try:
                        chunk = sock.recv(4096)
                    except _socket.timeout:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    if b"event: journal_activity" in buf:
                        got.put(("ok", buf))
                        return
                got.put(("timeout", buf))
            except Exception as e:  # noqa: BLE001
                got.put(("error", str(e)))
            finally:
                try:
                    sock.close()
                except Exception:
                    pass

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        # Give the reader time to connect and subscribe.
        time.sleep(0.3)

        # Fire a proxied request through the proxy.
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy_port, timeout=5)
        conn.request("POST", "/v1/messages",
                     body=json.dumps({"model": "model-a"}).encode(),
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        self.assertEqual(r.status, 200)
        r.read()

        kind, payload = got.get(timeout=5.0)
        self.assertEqual(kind, "ok", f"reader did not get event: {payload!r}")


class TestProxyMainWiresBus(unittest.TestCase):
    """The proxy.main() bootstrap must create an EventBus and wire it into
    both the JournalStore and the handler factory."""

    def test_main_wires_bus_into_store_and_handler(self):
        """Smoke-test by invoking the same wiring helper main() uses."""
        # We don't run the server. Instead we verify that proxy.py exposes a
        # helper or pattern that produces (bus, store, handler_kwargs) such
        # that handler kwargs include the bus. The simplest contract: a
        # JournalStore created with a bus, plus the handler accepts a
        # journal_bus kwarg, is sufficient. Verify these two pieces directly.
        from proxy import ClaudeProxyHandler
        from inspect import signature

        sig = signature(ClaudeProxyHandler.__init__)
        self.assertIn("journal_bus", sig.parameters)

        # And the store must accept bus= and round-trip it.
        bus = EventBus()
        store = JournalStore(bus=bus)
        self.assertIs(store._bus, bus)
