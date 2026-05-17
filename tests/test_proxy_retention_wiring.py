"""Tests that proxy.main() wires journal retention config + background tick
+ SIGTERM-driven shutdown of the eviction reaper.

main() is invoked with mocked I/O so it returns immediately after wiring.
"""

import json
import os
import signal
import sys
import tempfile
import unittest
import unittest.mock as mock

import proxy as proxy_module


def _write_minimal_configs(tmpdir):
    proxy_cfg = os.path.join(tmpdir, "proxy.json")
    models = os.path.join(tmpdir, "models.json")
    classification = os.path.join(tmpdir, "classification.json")
    journal = os.path.join(tmpdir, "journal.json")
    with open(proxy_cfg, "w") as f:
        json.dump({"upstream_timeout_seconds": 300}, f)
    with open(models, "w") as f:
        json.dump({"default": {"urls": [{"url": "http://up:8080"}]}}, f)
    with open(classification, "w") as f:
        json.dump({"enabled": False, "default": "CODING", "min_confidence": 0.5,
                   "labels": {"CODING": ["kw"]}}, f)
    with open(journal, "w") as f:
        json.dump({"enabled": True, "max_bytes": 4096, "max_age_seconds": 30}, f)
    return proxy_cfg, models, classification, journal


class TestProxyMainWiresRetentionAndReaper(unittest.TestCase):
    def test_main_passes_retention_to_store_and_starts_then_stops_reaper(self):
        with tempfile.TemporaryDirectory() as d:
            proxy_cfg, models, classification, journal = _write_minimal_configs(d)
            argv = [
                "proxy.py",
                "--port", "0",
                "--proxy-config", proxy_cfg,
                "--models-config", models,
                "--classification-config", classification,
                "--journal-config", journal,
            ]

            captured = {}

            real_store_cls = proxy_module.JournalStore

            class TrackingStore(real_store_cls):
                def __init__(self, *args, **kwargs):
                    captured["init_kwargs"] = kwargs
                    super().__init__(*args, **kwargs)
                    captured["instance"] = self

                def start_eviction_tick(self, *args, **kwargs):
                    captured["start_called"] = True
                    captured["start_kwargs"] = kwargs
                    return super().start_eviction_tick(*args, **kwargs)

                def stop_eviction_tick(self, *args, **kwargs):
                    captured["stop_called"] = True
                    return super().stop_eviction_tick(*args, **kwargs)

            # Mock the server so main() exits immediately after wiring.
            mock_server = mock.MagicMock()
            mock_server.serve_forever.side_effect = KeyboardInterrupt
            server_cls = mock.MagicMock(return_value=mock_server)

            captured_handler = {}

            def fake_sig(sig, handler):
                captured_handler[sig] = handler

            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(proxy_module, "JournalStore", TrackingStore), \
                 mock.patch.object(proxy_module, "ThreadingHTTPServer", server_cls), \
                 mock.patch.object(proxy_module.signal, "signal", side_effect=fake_sig):
                proxy_module.main()

            # Constructor received retention kwargs from journal.json.
            self.assertEqual(captured["init_kwargs"].get("max_bytes"), 4096)
            self.assertEqual(captured["init_kwargs"].get("max_age_seconds"), 30)
            # Background tick was started during wiring.
            self.assertTrue(captured.get("start_called"))

            # Invoke the SIGTERM handler the proxy registered — it must stop
            # the reaper.
            handler = captured_handler.get(signal.SIGTERM)
            self.assertIsNotNone(handler)
            handler(signal.SIGTERM, None)
            self.assertTrue(captured.get("stop_called"))


if __name__ == "__main__":
    unittest.main()
