"""Unit tests for journal.api.dispatch."""

import json
import unittest

from journal.api import dispatch
from journal.store import JournalStore


def _make_store_with_proxy_launched(entry_id="abc"):
    store = JournalStore()
    store.append(
        {
            "id": entry_id,
            "type": "proxy_launched",
            "ts": "2026-05-14T00:00:00Z",
            "pid": 12345,
            "port": 11435,
        },
        pinned=True,
    )
    return store


class TestJournalApiList(unittest.TestCase):
    def test_get_entries_returns_200_and_json_list(self):
        store = _make_store_with_proxy_launched()
        status, headers, body = dispatch("GET", "/__journal/entries", store)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        decoded = json.loads(body)
        self.assertIsInstance(decoded, list)
        self.assertEqual(len(decoded), 1)
        self.assertEqual(decoded[0]["id"], "abc")
        self.assertEqual(decoded[0]["type"], "proxy_launched")

    def test_entries_without_active_sessions_field(self):
        """Entries written at request completion time carry their own
        active_sessions field — no per-query injection needed.
        """
        store = _make_store_with_proxy_launched()
        store.append(
            {
                "id": "conn1",
                "type": "proxy_connection",
                "ts": "2026-05-14T01:00:00Z",
                "state": "SUCCESS",
                "active_sessions": 7,  # recorded at request finish
            }
        )
        status, _, body = dispatch("GET", "/__journal/entries", store)
        self.assertEqual(status, 200)
        decoded = json.loads(body)
        self.assertEqual(len(decoded), 2)
        self.assertEqual(decoded[1]["active_sessions"], 7)

    def test_entries_preserve_active_sessions_from_proxy(self):
        """Per-query active_count parameter is no longer accepted;
        active_sessions must already be present in the entry.
        """
        store = _make_store_with_proxy_launched()
        status, _, body = dispatch("GET", "/__journal/entries", store)
        self.assertEqual(status, 200)
        decoded = json.loads(body)
        # Old entries (pre-fix) have no active_sessions — they fall
        # back to entry_idx + 1 in the UI layer.
        self.assertNotIn("active_sessions", decoded[0])


class TestJournalApiGetById(unittest.TestCase):
    def test_get_entry_by_id_returns_200_and_full_entry(self):
        store = _make_store_with_proxy_launched(entry_id="zzz")
        status, headers, body = dispatch("GET", "/__journal/entries/zzz", store)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        decoded = json.loads(body)
        self.assertEqual(decoded["id"], "zzz")
        self.assertEqual(decoded["pid"], 12345)
        self.assertEqual(decoded["port"], 11435)

    def test_unknown_id_returns_404_with_error_json(self):
        store = _make_store_with_proxy_launched()
        status, headers, body = dispatch("GET", "/__journal/entries/nope", store)
        self.assertEqual(status, 404)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        decoded = json.loads(body)
        self.assertIn("error", decoded)


class TestJournalApiGpu(unittest.TestCase):
    def test_get_gpu_returns_empty_object_when_no_provider(self):
        store = _make_store_with_proxy_launched()
        status, headers, body = dispatch("GET", "/__journal/gpu", store)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(json.loads(body), {})

    def test_get_gpu_returns_empty_object_when_provider_returns_empty(self):
        store = _make_store_with_proxy_launched()
        status, _, body = dispatch(
            "GET", "/__journal/gpu", store, gpu_provider=lambda: {},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {})

    def test_get_gpu_returns_latest_sample_when_provider_has_one(self):
        store = _make_store_with_proxy_launched()
        sample = {
            "id": "g1", "type": "gpu_sample", "ts": "2026-05-14T01:00:00Z",
            "gpus": [{"index": 0, "name": "T4", "utilization_gpu": 42,
                      "memory_used_mib": 100, "memory_total_mib": 16000,
                      "temperature_c": 41, "power_draw_w": 35.2}],
        }
        status, _, body = dispatch(
            "GET", "/__journal/gpu", store, gpu_provider=lambda: sample,
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), sample)


class TestJournalApiSystem(unittest.TestCase):
    def test_get_system_returns_empty_object_when_no_provider(self):
        store = _make_store_with_proxy_launched()
        status, headers, body = dispatch("GET", "/__journal/system", store)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(json.loads(body), {})

    def test_get_system_returns_empty_object_when_provider_returns_empty(self):
        store = _make_store_with_proxy_launched()
        status, _, body = dispatch(
            "GET", "/__journal/system", store, system_provider=lambda: {},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {})

    def test_get_system_returns_latest_sample_when_provider_has_one(self):
        store = _make_store_with_proxy_launched()
        sample = {
            "ts": "2026-05-14T01:00:00Z",
            "uptime_seconds": 123.4,
            "loadavg": [0.1, 0.2, 0.3],
            "memory": {"total_bytes": 1024, "available_bytes": 768,
                       "used_bytes": 256, "free_bytes": 512},
            "disks": [{"mount": "/", "total_bytes": 100,
                       "used_bytes": 40, "free_bytes": 60}],
        }
        status, _, body = dispatch(
            "GET", "/__journal/system", store, system_provider=lambda: sample,
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), sample)


if __name__ == "__main__":
    unittest.main()
