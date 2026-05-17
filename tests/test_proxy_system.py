"""E2E test: SystemInfoPoller wired into the proxy → /__journal/system reflects."""

import http.client
import json
import socket
import threading
import time
import unittest
import uuid

from journal.bus import EventBus
from journal.pollers import SystemInfoPoller
from journal.store import JournalStore


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        return s.getsockname()[1]


def _sys(used):
    return {
        "uptime_seconds": 200.0,
        "loadavg": [0.1, 0.2, 0.3],
        "memory": {"total_bytes": 1024, "available_bytes": 768,
                   "used_bytes": 256, "free_bytes": 512},
        "disks": [{"mount": "/", "total_bytes": 100,
                   "used_bytes": used, "free_bytes": 100 - used}],
    }


class TestProxySystemEndpointE2E(unittest.TestCase):
    def setUp(self):
        from proxy import ClaudeProxyHandler, ThreadingHTTPServer

        self.bus = EventBus()
        self.store = JournalStore(bus=self.bus)
        self.sample = _sys(used=55)

        def sampler():
            return self.sample

        self.emitted = threading.Event()

        def on_sample(sample):
            entry = {"id": uuid.uuid4().hex, "type": "system_sample", **sample}
            self.store.append(entry)
            self.bus.publish("system_change", entry)
            self.emitted.set()

        self.stop = threading.Event()
        self.poller = SystemInfoPoller(
            sampler=sampler, on_sample=on_sample,
            interval=0.01, stop_event=self.stop,
        )
        self.poller_thread = self.poller.start()

        self.proxy_port = _free_port()
        store_ref = self.store
        bus_ref = self.bus
        poller_ref = self.poller
        self.proxy = ThreadingHTTPServer(
            ("127.0.0.1", self.proxy_port),
            lambda *a, **kw: ClaudeProxyHandler(
                *a, models={}, journal_store=store_ref,
                journal_bus=bus_ref, system_poller=poller_ref, **kw,
            ),
        )
        self.proxy_t = threading.Thread(target=self.proxy.serve_forever, daemon=True)
        self.proxy_t.start()

    def tearDown(self):
        self.stop.set()
        self.poller_thread.join(timeout=2.0)
        self.proxy.shutdown()
        self.proxy.server_close()

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy_port, timeout=5)
        conn.request("GET", path)
        r = conn.getresponse()
        body = r.read()
        return r.status, body

    def test_system_endpoint_reflects_latest_sample(self):
        self.assertTrue(self.emitted.wait(timeout=2.0),
                        "poller never invoked on_sample")
        deadline = time.time() + 2.0
        body = b"{}"
        while time.time() < deadline:
            status, body = self._get("/__journal/system")
            self.assertEqual(status, 200)
            decoded = json.loads(body)
            if decoded.get("disks"):
                break
            time.sleep(0.02)
        decoded = json.loads(body)
        self.assertIn("ts", decoded)
        self.assertEqual(decoded["disks"][0]["used_bytes"], 55)
        self.assertEqual(decoded["loadavg"], [0.1, 0.2, 0.3])
        self.assertEqual(decoded["memory"]["total_bytes"], 1024)


if __name__ == "__main__":
    unittest.main()
