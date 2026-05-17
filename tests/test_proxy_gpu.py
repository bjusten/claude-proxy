"""E2E test: NvidiaSmiPoller wired into the proxy → /__journal/gpu reflects."""

import http.client
import json
import socket
import threading
import time
import unittest
import uuid

from journal.bus import EventBus
from journal.pollers import NvidiaSmiPoller
from journal.store import JournalStore


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        return s.getsockname()[1]


def _gpu(util):
    return [{
        "index": 0, "name": "RTX A6000",
        "utilization_gpu": util,
        "memory_used_mib": 1000, "memory_total_mib": 49000,
        "temperature_c": 60, "power_draw_w": 100.0,
    }]


class TestProxyGpuEndpointE2E(unittest.TestCase):
    def setUp(self):
        from proxy import ClaudeProxyHandler, ThreadingHTTPServer

        self.bus = EventBus()
        self.store = JournalStore(bus=self.bus)
        # Sampler returns a stable changed sample after first call (so the
        # poller emits exactly once and `latest` is populated).
        self.gpus = _gpu(77)
        self.sampler_calls = 0

        def sampler():
            self.sampler_calls += 1
            return self.gpus

        self.emitted = threading.Event()
        self.published = []

        def on_sample(sample):
            entry = {"id": uuid.uuid4().hex, "type": "gpu_sample", **sample}
            self.store.append(entry)
            self.bus.publish("gpu_change", entry)
            self.emitted.set()

        self.stop = threading.Event()
        self.poller = NvidiaSmiPoller(
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
                journal_bus=bus_ref, gpu_poller=poller_ref, **kw,
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

    def test_gpu_endpoint_reflects_latest_sample(self):
        # Wait until the poller has emitted at least once.
        self.assertTrue(self.emitted.wait(timeout=2.0),
                        "poller never invoked on_sample")
        # Allow the latest property to be set after on_sample dispatch.
        deadline = time.time() + 2.0
        body = b"{}"
        while time.time() < deadline:
            status, body = self._get("/__journal/gpu")
            self.assertEqual(status, 200)
            decoded = json.loads(body)
            if decoded.get("gpus"):
                break
            time.sleep(0.02)
        decoded = json.loads(body)
        self.assertIn("ts", decoded)
        self.assertEqual(decoded["gpus"][0]["utilization_gpu"], 77)


if __name__ == "__main__":
    unittest.main()
