"""Tests for journal capture of proxied requests in ClaudeProxyHandler.

These tests exercise the handler with a mocked urlopen and a journal_store,
verifying that proxy_connection entries are appended with correct envelopes
and phase timestamps.
"""

import io
import json
import os
import socket
import tempfile
import threading
import unittest
import unittest.mock as mock
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from http.server import HTTPServer
from urllib.error import URLError, HTTPError

from proxy import ClaudeProxyHandler, ThreadingHTTPServer, _active_sessions, _rr, _session_logger
from journal.store import JournalStore


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        return s.getsockname()[1]


def _ok_resp(body=b"upstream response", status=200, headers=None):
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


def _build_handler(*, config, journal_store, method="POST", path="/v1/messages",
                   body=None, headers=None, classification_config=None):
    if isinstance(body, bytes):
        body_bytes = body
    elif body is not None:
        body_bytes = json.dumps(body).encode()
    else:
        body_bytes = b""

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
    hdrs = dict(headers or {})
    if "Content-Length" not in hdrs:
        hdrs["Content-Length"] = str(len(body_bytes))
    handler.headers = hdrs
    handler.rfile = io.BytesIO(body_bytes)
    handler.wfile = io.BytesIO()
    handler.journal_store = journal_store
    if classification_config is not None:
        handler.classification_config = classification_config
    return handler


class TestProxyConnectionShallow(unittest.TestCase):
    def setUp(self):
        _rr._active.clear()
        _active_sessions._count = 0
        for h in _session_logger.handlers[:]:
            _session_logger.removeHandler(h)

    def test_one_proxy_connection_entry_with_summary_fields(self):
        store = JournalStore()
        config = {"model-a": {"urls": [{"url": "http://up:8080", "new_model_name": "llama3:70b"}]}}
        handler = _build_handler(
            config=config,
            journal_store=store,
            method="POST",
            path="/v1/messages",
            body={"model": "model-a", "messages": []},
        )
        with mock.patch("urllib.request.urlopen", return_value=_ok_resp(body=b"hello")):
            handler.do_POST()

        # Filter to proxy_connection entries (proxy_launched may not be present here)
        entries = [e for e in store.list_shallow() if e["type"] == "proxy_connection"]
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["method"], "POST")
        self.assertEqual(e["path"], "/v1/messages")
        self.assertEqual(e["original_model"], "model-a")
        self.assertIsNone(e["classified"])
        self.assertEqual(e["destination_url"], "http://up:8080")
        self.assertEqual(e["status"], 200)
        self.assertEqual(e["bytes"], 5)


class TestProxyConnectionDeep(unittest.TestCase):
    def setUp(self):
        _rr._active.clear()
        _active_sessions._count = 0
        for h in _session_logger.handlers[:]:
            _session_logger.removeHandler(h)

    def test_deep_entry_contains_full_envelopes(self):
        store = JournalStore()
        config = {"model-a": {"urls": [{"url": "http://up:8080", "new_model_name": "llama3:70b"}]}}
        handler = _build_handler(
            config=config,
            journal_store=store,
            method="POST",
            path="/v1/messages",
            body={"model": "model-a", "messages": []},
            headers={"X-Custom": "abc"},
        )
        with mock.patch(
            "urllib.request.urlopen",
            return_value=_ok_resp(body=b"hello", status=200,
                                  headers=[("Content-Type", "application/json")]),
        ):
            handler.do_POST()

        shallow = [e for e in store.list_shallow() if e["type"] == "proxy_connection"]
        entry_id = shallow[0]["id"]
        deep = store.get_deep(entry_id)

        # Incoming envelope
        self.assertEqual(deep["incoming"]["method"], "POST")
        self.assertEqual(deep["incoming"]["path"], "/v1/messages")
        self.assertIn("X-Custom", deep["incoming"]["headers"])
        self.assertTrue(deep["incoming"]["body"])

        # Routed envelope
        self.assertEqual(deep["routed"]["url"], "http://up:8080")
        self.assertIsInstance(deep["routed"]["headers"], dict)
        self.assertTrue(deep["routed"]["body"])
        self.assertEqual(deep["routed"]["original_model"], "model-a")
        self.assertEqual(deep["routed"]["new_model"], "llama3:70b")
        self.assertEqual(deep["routed"]["routing"]["mode"], "model")
        self.assertEqual(deep["routed"]["routing"]["entry_idx"], -1)

        # Response envelope
        self.assertEqual(deep["response"]["status"], 200)
        self.assertIsInstance(deep["response"]["headers"], dict)
        self.assertEqual(deep["response"]["body"], b"hello")
        self.assertEqual(deep["response"]["bytes"], 5)


class TestProxyConnectionRouting(unittest.TestCase):
    def setUp(self):
        _rr._active.clear()
        _active_sessions._count = 0
        for h in _session_logger.handlers[:]:
            _session_logger.removeHandler(h)

    def test_classification_enabled_records_classified_and_scores(self):
        store = JournalStore()
        config = {"code": {"urls": [{"url": "http://up:8080", "new_model_name": "llama3:70b"}]}}
        cls_cfg = {"enabled": True, "default": "code", "min_confidence": 0.0,
                   "budget_warn_ms": 1000}
        handler = _build_handler(
            config=config,
            journal_store=store,
            body={"model": "model-a", "messages": []},
            classification_config=cls_cfg,
        )
        fake_scores = {"code": 8.0, "reasoning": 1.0, "scan_ms": 0.5}
        with mock.patch("classifier.classify", return_value=("code", fake_scores)):
            with mock.patch("urllib.request.urlopen", return_value=_ok_resp(body=b"x")):
                handler.do_POST()

        shallow = [e for e in store.list_shallow() if e["type"] == "proxy_connection"]
        deep = store.get_deep(shallow[0]["id"])
        routing = deep["routed"]["routing"]
        self.assertEqual(routing["mode"], "classified")
        self.assertEqual(routing["classified"], "code")
        self.assertEqual(routing["scores"], fake_scores)
        # And shallow projection surfaces 'classified'
        self.assertEqual(shallow[0]["classified"], "code")
        # And classified timestamp recorded
        self.assertIn("classified", deep["timestamps"])

    def test_classification_disabled_uses_model_mode(self):
        store = JournalStore()
        config = {"model-a": {"urls": [{"url": "http://up:8080", "new_model_name": "llama3:70b"}]}}
        handler = _build_handler(
            config=config,
            journal_store=store,
            body={"model": "model-a"},
        )
        with mock.patch("urllib.request.urlopen", return_value=_ok_resp(body=b"x")):
            handler.do_POST()

        shallow = [e for e in store.list_shallow() if e["type"] == "proxy_connection"]
        deep = store.get_deep(shallow[0]["id"])
        routing = deep["routed"]["routing"]
        self.assertEqual(routing["mode"], "model")
        self.assertNotIn("scores", routing)
        self.assertIsNone(shallow[0]["classified"])


class TestClassificationRequestJournaled(unittest.TestCase):
    """LLM classification calls land in the journal under their own session."""

    def setUp(self):
        _rr._active.clear()
        _active_sessions._count = 0
        for h in _session_logger.handlers[:]:
            _session_logger.removeHandler(h)

    def _llm_cls_cfg(self):
        import classifier
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump({"enabled": True, "method": "llm",
                   "llm_url": "http://llm:9/v1/chat/completions",
                   "llm_model": "qwen"}, f)
        f.close()
        try:
            return classifier.load_config(f.name)
        finally:
            os.unlink(f.name)

    def test_llm_classification_appended_as_own_session(self):
        import classifier
        store = JournalStore()
        config = {"CODING": {"urls": [{"url": "http://up:8080", "new_model_name": "llama3:70b"}]}}
        handler = _build_handler(
            config=config,
            journal_store=store,
            body={"model": "model-a", "messages": [{"role": "user", "content": "hi"}]},
            headers={"anthropic-session-id": "user-sess"},
            classification_config=self._llm_cls_cfg(),
        )
        with mock.patch.object(classifier, "_call_llm",
                               return_value=('{"choices":[{"message":{"content":"coding"}}]}', {})):
            with mock.patch("urllib.request.urlopen", return_value=_ok_resp(body=b"x")):
                handler.do_POST()

        entries = [e for e in store.list_shallow() if e["type"] == "proxy_connection"]
        by_sess = {e["session_id"]: e for e in entries}
        self.assertIn("classification requests", by_sess)
        self.assertIn("user-sess", by_sess)

        cls_entry = by_sess["classification requests"]
        self.assertEqual(cls_entry["method"], "POST")
        self.assertEqual(cls_entry["path"], "/v1/chat/completions")
        self.assertEqual(cls_entry["classified"], "CODING")
        self.assertEqual(cls_entry["status"], 200)

        deep = store.get_deep(cls_entry["id"])
        self.assertNotIn("routed", deep)
        self.assertIn("classification", deep)
        self.assertEqual(deep["classification"]["classified"], "CODING")
        self.assertIn("hi", deep["incoming"]["body"])

    def test_no_classification_entry_when_method_internal(self):
        store = JournalStore()
        config = {"code": {"urls": [{"url": "http://up:8080", "new_model_name": "m"}]}}
        cls_cfg = {"enabled": True, "default": "code", "min_confidence": 0.0,
                   "budget_warn_ms": 1000, "method": "internal"}
        handler = _build_handler(
            config=config,
            journal_store=store,
            body={"model": "model-a", "messages": []},
            classification_config=cls_cfg,
        )
        with mock.patch("urllib.request.urlopen", return_value=_ok_resp(body=b"x")):
            handler.do_POST()

        entries = [e for e in store.list_shallow() if e["type"] == "proxy_connection"]
        sessions = {e["session_id"] for e in entries}
        self.assertNotIn("classification requests", sessions)


class TestProxyConnectionSessionCorrelation(unittest.TestCase):
    def setUp(self):
        _rr._active.clear()
        _active_sessions._count = 0
        for h in _session_logger.handlers[:]:
            _session_logger.removeHandler(h)

    def test_same_trace_header_groups_under_same_session_id(self):
        store = JournalStore()
        config = {"model-a": {"urls": [{"url": "http://up:8080", "new_model_name": "x"}]}}
        for _ in range(2):
            handler = _build_handler(
                config=config,
                journal_store=store,
                body={"model": "model-a"},
                headers={"anthropic-session-id": "shared-sess"},
            )
            with mock.patch("urllib.request.urlopen", return_value=_ok_resp(body=b"x")):
                handler.do_POST()

        entries = [e for e in store.list_shallow() if e["type"] == "proxy_connection"]
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["session_id"], "shared-sess")
        self.assertEqual(entries[1]["session_id"], "shared-sess")


class TestProxyConnectionErrors(unittest.TestCase):
    def setUp(self):
        _rr._active.clear()
        _active_sessions._count = 0
        for h in _session_logger.handlers[:]:
            _session_logger.removeHandler(h)

    def test_urlerror_records_error_kind_and_message(self):
        store = JournalStore()
        config = {"model-a": {"urls": [{"url": "http://up:8080", "new_model_name": "x"}]}}
        handler = _build_handler(
            config=config,
            journal_store=store,
            body={"model": "model-a"},
        )
        with mock.patch("urllib.request.urlopen", side_effect=URLError("unreachable")):
            handler.do_POST()

        shallow = [e for e in store.list_shallow() if e["type"] == "proxy_connection"]
        deep = store.get_deep(shallow[0]["id"])
        self.assertIsNotNone(deep["error"])
        self.assertEqual(deep["error"]["kind"], "URLError")
        self.assertIn("unreachable", deep["error"]["message"])

    def test_httperror_records_error_kind_and_status(self):
        store = JournalStore()
        config = {"model-a": {"urls": [{"url": "http://up:8080", "new_model_name": "x"}]}}
        handler = _build_handler(
            config=config,
            journal_store=store,
            body={"model": "model-a"},
        )
        err = HTTPError(
            url="http://up:8080/v1/messages",
            code=503,
            msg="Service Unavailable",
            hdrs={"Content-Type": "text/plain"},
            fp=io.BytesIO(b"down"),
        )
        with mock.patch("urllib.request.urlopen", side_effect=err):
            handler.do_POST()

        shallow = [e for e in store.list_shallow() if e["type"] == "proxy_connection"]
        deep = store.get_deep(shallow[0]["id"])
        self.assertIsNotNone(deep["error"])
        self.assertEqual(deep["error"]["kind"], "HTTPError")
        self.assertIn("503", deep["error"]["message"])
        # Response status from HTTPError is recorded
        self.assertEqual(deep["response"]["status"], 503)


class TestProxyConnectionTimestamps(unittest.TestCase):
    def setUp(self):
        _rr._active.clear()
        _active_sessions._count = 0
        for h in _session_logger.handlers[:]:
            _session_logger.removeHandler(h)

    def test_phase_timestamps_present_and_ordered(self):
        store = JournalStore()
        config = {"model-a": {"urls": [{"url": "http://up:8080", "new_model_name": "llama3:70b"}]}}
        handler = _build_handler(
            config=config,
            journal_store=store,
            body={"model": "model-a"},
        )
        with mock.patch("urllib.request.urlopen", return_value=_ok_resp(body=b"x")):
            handler.do_POST()

        shallow = [e for e in store.list_shallow() if e["type"] == "proxy_connection"]
        deep = store.get_deep(shallow[0]["id"])
        ts = deep["timestamps"]
        for k in ("received", "routed", "response_started", "response_completed"):
            self.assertIn(k, ts, f"missing timestamp {k}")
        # ISO strings sort lexicographically by time
        self.assertLessEqual(ts["received"], ts["routed"])
        self.assertLessEqual(ts["routed"], ts["response_started"])
        self.assertLessEqual(ts["response_started"], ts["response_completed"])


class _MockUpstreamHandler(BaseHTTPRequestHandler):
    """Tiny in-process upstream that echoes a fixed response."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        _ = self.rfile.read(length)
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args, **_kw):
        pass


class _ThreadingUpstream(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class TestProxyConnectionE2E(unittest.TestCase):
    """End-to-end: proxy live server in front of an in-process mock upstream."""

    def setUp(self):
        _rr._active.clear()
        _active_sessions._count = 0
        for h in _session_logger.handlers[:]:
            _session_logger.removeHandler(h)

        self.up_port = _free_port()
        self.up = _ThreadingUpstream(("127.0.0.1", self.up_port), _MockUpstreamHandler)
        self.up_t = threading.Thread(target=self.up.serve_forever, daemon=True)
        self.up_t.start()

        self.store = JournalStore()
        self.proxy_port = _free_port()
        config = {"model-a": {"urls": [{"url": f"http://127.0.0.1:{self.up_port}",
                                         "new_model_name": "llama3:70b"}]}}
        self.proxy = ThreadingHTTPServer(
            ("127.0.0.1", self.proxy_port),
            lambda *a, **kw: ClaudeProxyHandler(*a, models=config, journal_store=self.store, **kw),
        )
        self.proxy_t = threading.Thread(target=self.proxy.serve_forever, daemon=True)
        self.proxy_t.start()

    def tearDown(self):
        self.proxy.shutdown()
        self.proxy.server_close()
        self.up.shutdown()
        self.up.server_close()

    def _post(self, path, body):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy_port, timeout=5)
        conn.request("POST", path, body=json.dumps(body).encode(),
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        return r.status, r.read()

    def _get(self, path):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy_port, timeout=5)
        conn.request("GET", path)
        r = conn.getresponse()
        return r.status, r.read()

    def test_proxied_request_appears_in_shallow_and_deep(self):
        status, body = self._post("/v1/messages", {"model": "model-a", "messages": []})
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"ok":true}')

        list_status, list_body = self._get("/__journal/entries")
        self.assertEqual(list_status, 200)
        entries = json.loads(list_body)
        conns = [e for e in entries if e["type"] == "proxy_connection"]
        self.assertEqual(len(conns), 1)
        c = conns[0]
        self.assertEqual(c["method"], "POST")
        self.assertEqual(c["path"], "/v1/messages")
        self.assertEqual(c["status"], 200)
        self.assertEqual(c["bytes"], len(b'{"ok":true}'))

        deep_status, deep_body = self._get(f"/__journal/entries/{c['id']}")
        self.assertEqual(deep_status, 200)
        deep = json.loads(deep_body)
        self.assertEqual(deep["incoming"]["method"], "POST")
        self.assertEqual(deep["routed"]["url"], f"http://127.0.0.1:{self.up_port}")
        self.assertEqual(deep["response"]["status"], 200)


class TestProxyConnectionTokens(unittest.TestCase):
    """extract_usage runs at response-capture and is journaled as `tokens`."""

    def setUp(self):
        _rr._active.clear()
        _active_sessions._count = 0
        for h in _session_logger.handlers[:]:
            _session_logger.removeHandler(h)

    def test_anthropic_json_usage_captured(self):
        store = JournalStore()
        config = {"default": {"urls": [{"url": "http://up:8080"}]}}
        handler = _build_handler(
            config=config,
            journal_store=store,
            method="POST",
            path="/v1/messages",
            body={"model": "claude-sonnet-4-6"},
        )
        body = json.dumps({
            "id": "msg_1",
            "usage": {"input_tokens": 11, "output_tokens": 22},
        }).encode()
        with mock.patch("urllib.request.urlopen", return_value=_ok_resp(body=body)):
            handler.do_POST()

        shallow = [e for e in store.list_shallow() if e["type"] == "proxy_connection"]
        self.assertEqual(len(shallow), 1)
        self.assertEqual(shallow[0]["tokens"], {"input": 11, "output": 22})
        deep = store.get_deep(shallow[0]["id"])
        self.assertEqual(deep["tokens"], {"input": 11, "output": 22})

    def test_sse_anthropic_usage_captured(self):
        store = JournalStore()
        config = {"default": {"urls": [{"url": "http://up:8080"}]}}
        handler = _build_handler(
            config=config,
            journal_store=store,
            method="POST",
            path="/v1/messages",
            body={"model": "claude-sonnet-4-6"},
        )
        body = (
            b'event: message_start\n'
            b'data: {"type":"message_start","message":{"id":"m","usage":{"input_tokens":7,"output_tokens":1}}}\n\n'
            b'event: message_delta\n'
            b'data: {"type":"message_delta","usage":{"output_tokens":13}}\n\n'
        )
        sse_headers = [("Content-Type", "text/event-stream")]
        with mock.patch("urllib.request.urlopen", return_value=_ok_resp(body=body, headers=sse_headers)):
            handler.do_POST()

        shallow = [e for e in store.list_shallow() if e["type"] == "proxy_connection"]
        self.assertEqual(len(shallow), 1)
        self.assertEqual(shallow[0]["tokens"], {"input": 7, "output": 13})


if __name__ == "__main__":
    unittest.main()
