"""
Unit tests for journal.store.JournalStore.

Tests cover the public interface:
  - append(entry, pinned=False) -> id
  - list_shallow() -> list of shallow entries
  - get_deep(id) -> full entry | None
  - pinned_entry() -> full pinned entry | None
"""

import unittest

from journal.store import JournalStore


class TestJournalStoreAppendList(unittest.TestCase):
    def test_append_then_list_shallow_returns_entry(self):
        store = JournalStore()
        entry = {"id": "abc", "type": "proxy_launched", "ts": "2026-05-14T00:00:00Z", "pid": 1, "port": 11435}
        returned_id = store.append(entry)
        self.assertEqual(returned_id, "abc")
        listed = store.list_shallow()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], "abc")
        self.assertEqual(listed[0]["type"], "proxy_launched")
        self.assertEqual(listed[0]["pid"], 1)
        self.assertEqual(listed[0]["port"], 11435)


class TestJournalStoreGetDeep(unittest.TestCase):
    def test_get_deep_returns_full_entry(self):
        store = JournalStore()
        entry = {
            "id": "xyz",
            "type": "proxy_launched",
            "ts": "2026-05-14T00:00:00Z",
            "pid": 42,
            "port": 11435,
            "config_paths": {"config": "/c", "classification": "/cl", "journal": "/j"},
            "versions": {"python": "3.13.5"},
        }
        store.append(entry)
        deep = store.get_deep("xyz")
        self.assertEqual(deep["config_paths"]["config"], "/c")
        self.assertEqual(deep["versions"]["python"], "3.13.5")

    def test_get_deep_unknown_id_returns_none(self):
        store = JournalStore()
        self.assertIsNone(store.get_deep("missing"))


class TestJournalStoreUpdate(unittest.TestCase):
    def test_update_merges_top_level_fields_and_returns_true(self):
        store = JournalStore()
        store.append({"id": "u1", "type": "proxy_connection", "status": None})
        ok = store.update("u1", {"status": 200})
        self.assertTrue(ok)
        deep = store.get_deep("u1")
        self.assertEqual(deep["status"], 200)
        # Pre-existing keys preserved
        self.assertEqual(deep["type"], "proxy_connection")

    def test_update_unknown_id_returns_false_and_changes_nothing(self):
        store = JournalStore()
        store.append({"id": "u1", "type": "proxy_connection", "status": None})
        ok = store.update("missing", {"status": 200})
        self.assertFalse(ok)
        deep = store.get_deep("u1")
        self.assertIsNone(deep["status"])

    def test_update_recursively_merges_nested_dicts(self):
        store = JournalStore()
        store.append({
            "id": "u1",
            "type": "proxy_connection",
            "timestamps": {"received": "2026-05-14T00:00:00Z"},
        })
        ok = store.update("u1", {"timestamps": {"routed": "2026-05-14T00:00:01Z"}})
        self.assertTrue(ok)
        deep = store.get_deep("u1")
        # Existing nested key preserved, new key added
        self.assertEqual(deep["timestamps"]["received"], "2026-05-14T00:00:00Z")
        self.assertEqual(deep["timestamps"]["routed"], "2026-05-14T00:00:01Z")


class TestJournalStoreShallowProjection(unittest.TestCase):
    def test_proxy_connection_shallow_projects_summary_fields(self):
        store = JournalStore()
        full_entry = {
            "id": "c1",
            "type": "proxy_connection",
            "session_id": "sess-abc",
            "timestamps": {
                "received": "2026-05-14T00:00:00Z",
                "routed": "2026-05-14T00:00:01Z",
            },
            "incoming": {
                "method": "POST",
                "path": "/v1/messages",
                "headers": {"x-secret": "deadbeef"},
                "body": b"verbatim",
            },
            "routed": {
                "url": "http://up:8080/v1/messages",
                "headers": {},
                "body": b"rewritten",
                "original_model": "model-a",
                "new_model": "llama3:70b",
                "routing": {"mode": "model", "entry_idx": -1},
            },
            "response": {"status": 200, "headers": {}, "body": b"resp", "bytes": 4},
            "error": None,
        }
        store.append(full_entry)
        listed = store.list_shallow()
        self.assertEqual(len(listed), 1)
        shallow = listed[0]
        # Required summary fields present
        self.assertEqual(shallow["id"], "c1")
        self.assertEqual(shallow["type"], "proxy_connection")
        self.assertEqual(shallow["ts"], "2026-05-14T00:00:00Z")
        self.assertEqual(shallow["sending_ts"], "2026-05-14T00:00:01Z")
        self.assertEqual(shallow["session_id"], "sess-abc")
        self.assertEqual(shallow["method"], "POST")
        self.assertEqual(shallow["path"], "/v1/messages")
        self.assertEqual(shallow["original_model"], "model-a")
        self.assertIsNone(shallow.get("classified"))
        self.assertEqual(shallow["destination_url"], "http://up:8080/v1/messages")
        self.assertEqual(shallow["status"], 200)
        self.assertEqual(shallow["bytes"], 4)
        # Verbatim headers / bodies NOT in shallow
        self.assertNotIn("incoming", shallow)
        self.assertNotIn("routed", shallow)
        self.assertNotIn("response", shallow)

    def test_proxy_launched_shallow_preserves_full_entry(self):
        store = JournalStore()
        entry = {
            "id": "p1",
            "type": "proxy_launched",
            "ts": "2026-05-14T00:00:00Z",
            "pid": 1,
            "port": 11435,
        }
        store.append(entry)
        listed = store.list_shallow()
        self.assertEqual(listed[0]["pid"], 1)
        self.assertEqual(listed[0]["port"], 11435)


class TestJournalStoreRetentionConstructor(unittest.TestCase):
    def test_constructor_accepts_max_bytes_and_max_age_seconds(self):
        store = JournalStore(max_bytes=1024, max_age_seconds=60)
        self.assertEqual(store._max_bytes, 1024)
        self.assertEqual(store._max_age_seconds, 60)

    def test_constructor_defaults_when_omitted(self):
        store = JournalStore()
        self.assertEqual(store._max_bytes, 1024 ** 3)  # 1 GiB
        self.assertEqual(store._max_age_seconds, 7200)  # 2 hours


class TestJournalStoreByteCapEviction(unittest.TestCase):
    def test_byte_cap_evicts_oldest_non_pinned_until_within_cap(self):
        # Tiny cap; each entry serialised JSON is around ~40-60 bytes.
        store = JournalStore(max_bytes=200, max_age_seconds=10_000)
        for i in range(10):
            store.append({"id": f"e{i}", "type": "proxy_connection", "n": i})
        listed = store.list_shallow()
        # The most recent entries should remain; total accounted bytes ≤ 200.
        ids = [e["id"] for e in listed]
        # Oldest entries evicted; newest preserved
        self.assertNotIn("e0", ids)
        self.assertIn("e9", ids)
        # Accounted bytes within cap
        total = sum(store._sizes_by_id[e["id"]] for e in listed)
        self.assertLessEqual(total, 200)


class TestJournalStoreTimeCapEviction(unittest.TestCase):
    def test_time_cap_evicts_expired_entries_on_next_append(self):
        # Fake clock under our control.
        now = [1000.0]
        store = JournalStore(max_bytes=10 ** 9, max_age_seconds=60,
                             clock=lambda: now[0])
        store.append({"id": "a", "type": "proxy_connection"})
        # Advance time past the cap, then append a fresh entry to trigger reap.
        now[0] += 61
        store.append({"id": "b", "type": "proxy_connection"})
        ids = [e["id"] for e in store.list_shallow()]
        self.assertNotIn("a", ids)
        self.assertIn("b", ids)


class TestJournalStorePinnedSurvivesByteCap(unittest.TestCase):
    def test_pinned_proxy_launched_survives_byte_pressure(self):
        store = JournalStore(max_bytes=200, max_age_seconds=10_000)
        store.append({"id": "pin", "type": "proxy_launched", "pid": 1, "port": 11435}, pinned=True)
        for i in range(20):
            store.append({"id": f"e{i}", "type": "proxy_connection", "n": i})
        self.assertIsNotNone(store.pinned_entry())
        self.assertEqual(store.pinned_entry()["id"], "pin")
        self.assertIsNotNone(store.get_deep("pin"))


class TestJournalStorePinnedSurvivesTimeCap(unittest.TestCase):
    def test_pinned_entry_survives_time_pressure(self):
        now = [1000.0]
        store = JournalStore(max_bytes=10 ** 9, max_age_seconds=60,
                             clock=lambda: now[0])
        store.append({"id": "pin", "type": "proxy_launched", "pid": 1, "port": 11435}, pinned=True)
        # Advance way past the time cap, append a fresh entry to trigger reap.
        now[0] += 10_000
        store.append({"id": "fresh", "type": "proxy_connection"})
        self.assertIsNotNone(store.pinned_entry())
        self.assertEqual(store.pinned_entry()["id"], "pin")


class TestJournalStoreBothCaps(unittest.TestCase):
    def test_both_caps_simultaneously_satisfied(self):
        now = [1000.0]
        store = JournalStore(max_bytes=200, max_age_seconds=60,
                             clock=lambda: now[0])
        # Append batch 1: small, but they'll age out.
        for i in range(3):
            store.append({"id": f"old{i}", "type": "proxy_connection", "n": i})
        # Advance past age cap.
        now[0] += 61
        # Append batch 2: enough to also exceed byte cap.
        for i in range(20):
            store.append({"id": f"new{i}", "type": "proxy_connection", "n": i})
        ids = [e["id"] for e in store.list_shallow()]
        # Old entries are gone (time cap)
        for i in range(3):
            self.assertNotIn(f"old{i}", ids)
        # Total byte size within cap
        total = sum(store._sizes_by_id[i] for i in ids)
        self.assertLessEqual(total, 200)


class TestJournalStoreUpdateRefreshesByteAccounting(unittest.TestCase):
    def test_update_refreshes_size_and_triggers_eviction(self):
        store = JournalStore(max_bytes=600, max_age_seconds=10_000)
        store.append({"id": "small1", "type": "proxy_connection"})
        store.append({"id": "small2", "type": "proxy_connection"})
        store.append({"id": "target", "type": "proxy_connection"})
        # Pre-update: comfortably under 700 bytes; nothing evicted.
        self.assertEqual(len(store.list_shallow()), 3)
        # Grow `target` so the total exceeds 700 bytes (target ~= 530 alone).
        big = "x" * 500
        store.update("target", {"payload": big})
        # Updated entry's cached size reflects the new content.
        self.assertGreater(store._sizes_by_id["target"], 400)
        # Target fits within the cap; eviction dropped older smalls to fit.
        ids = [e["id"] for e in store.list_shallow()]
        self.assertIn("target", ids)
        self.assertNotIn("small1", ids)
        total = sum(store._sizes_by_id[i] for i in ids)
        self.assertLessEqual(total, 600)


class TestJournalStoreBackgroundTick(unittest.TestCase):
    def test_background_tick_evicts_time_expired_on_idle_journal(self):
        import time as real_time
        now = [1000.0]
        store = JournalStore(max_bytes=10 ** 9, max_age_seconds=60,
                             clock=lambda: now[0])
        store.append({"id": "stale", "type": "proxy_connection"})
        store.start_eviction_tick(interval_seconds=0.02)
        try:
            # Advance fake clock past the age cap; journal is otherwise idle.
            now[0] += 61
            # Wait a handful of real ticks.
            deadline = real_time.time() + 1.0
            while real_time.time() < deadline:
                if not store.get_deep("stale"):
                    break
                real_time.sleep(0.02)
            self.assertIsNone(store.get_deep("stale"))
        finally:
            store.stop_eviction_tick()


class TestJournalStoreBackgroundTickShutdown(unittest.TestCase):
    def test_stop_eviction_tick_joins_thread_cleanly(self):
        store = JournalStore(max_bytes=10 ** 9, max_age_seconds=60)
        t = store.start_eviction_tick(interval_seconds=0.02)
        self.assertTrue(t.is_alive())
        store.stop_eviction_tick(timeout=1.0)
        self.assertFalse(t.is_alive())

    def test_stop_eviction_tick_is_safe_when_never_started(self):
        store = JournalStore()
        # Should not raise.
        store.stop_eviction_tick()


class TestJournalStorePinned(unittest.TestCase):
    def test_pinned_entry_returns_entry_appended_with_pinned_flag(self):
        store = JournalStore()
        launched = {"id": "p1", "type": "proxy_launched", "ts": "t", "pid": 1, "port": 11435}
        other = {"id": "o1", "type": "proxy_connection", "ts": "t"}
        store.append(launched, pinned=True)
        store.append(other)
        pinned = store.pinned_entry()
        self.assertIsNotNone(pinned)
        self.assertEqual(pinned["id"], "p1")
        self.assertEqual(pinned["type"], "proxy_launched")

    def test_pinned_entry_none_when_nothing_pinned(self):
        store = JournalStore()
        self.assertIsNone(store.pinned_entry())


class TestShallowProjectTokens(unittest.TestCase):
    def test_tokens_field_included_when_set(self):
        from journal.store import _shallow_project
        entry = {
            "id": "x",
            "type": "proxy_connection",
            "session_id": "s1",
            "timestamps": {"received": "2026-05-14T00:00:00Z"},
            "incoming": {"method": "POST", "path": "/v1/messages"},
            "routed": {"url": "http://u", "original_model": "m", "new_model": "m"},
            "response": {"status": 200, "bytes": 10},
            "tokens": {"input": 11, "output": 22},
        }
        shallow = _shallow_project(entry)
        self.assertEqual(shallow["tokens"], {"input": 11, "output": 22})

    def test_tokens_field_is_none_when_absent(self):
        from journal.store import _shallow_project
        entry = {
            "id": "x",
            "type": "proxy_connection",
            "session_id": "s1",
            "timestamps": {"received": "2026-05-14T00:00:00Z"},
            "incoming": {"method": "POST", "path": "/v1/messages"},
            "routed": {"url": "http://u", "original_model": "m", "new_model": "m"},
            "response": {"status": 200, "bytes": 10},
        }
        shallow = _shallow_project(entry)
        self.assertIsNone(shallow["tokens"])


class TestShallowProjectState(unittest.TestCase):
    def test_shallow_project_includes_state_for_proxy_connection(self):
        store = JournalStore()
        store.append({
            "id": "c1", "type": "proxy_connection", "session_id": "s1",
            "timestamps": {"received": "2026-05-16T00:00:00Z"},
            "incoming": {"method": "POST", "path": "/v1/messages"},
            "state": "ROUTING_REQUEST",
        })
        [row] = [e for e in store.list_shallow() if e["type"] == "proxy_connection"]
        self.assertEqual(row["state"], "ROUTING_REQUEST")

    def test_shallow_project_state_absent_is_none(self):
        store = JournalStore()
        store.append({
            "id": "c2", "type": "proxy_connection", "session_id": "s1",
            "timestamps": {"received": "2026-05-16T00:00:00Z"},
            "incoming": {"method": "POST", "path": "/v1/messages"},
        })
        [row] = [e for e in store.list_shallow() if e["type"] == "proxy_connection"]
        self.assertIsNone(row["state"])


class TestShallowProjectDuration(unittest.TestCase):
    def test_proxy_connection_duration_computed_from_timestamps(self):
        from journal.store import _shallow_project
        entry = {
            "id": "c1",
            "type": "proxy_connection",
            "session_id": "s1",
            "timestamps": {
                "received": "2026-05-14T00:00:00.000Z",
                "response_completed": "2026-05-14T00:00:01.500Z",
            },
            "incoming": {"method": "POST", "path": "/v1/messages"},
            "routed": {"url": "http://u", "original_model": "m", "new_model": "m"},
            "response": {"status": 200, "bytes": 10},
        }
        shallow = _shallow_project(entry)
        self.assertEqual(shallow["duration_ms"], 1500)

    def test_proxy_connection_duration_none_when_timestamps_empty(self):
        from journal.store import _shallow_project
        entry = {
            "id": "c2",
            "type": "proxy_connection",
            "session_id": "s1",
            "timestamps": {},
            "incoming": {"method": "POST", "path": "/v1/messages"},
            "routed": {"url": "http://u"},
            "response": {"status": 200},
        }
        shallow = _shallow_project(entry)
        self.assertIsNone(shallow["duration_ms"])

    def test_proxy_connection_duration_none_when_only_received(self):
        from journal.store import _shallow_project
        entry = {
            "id": "c3",
            "type": "proxy_connection",
            "session_id": "s1",
            "timestamps": {
                "received": "2026-05-14T00:00:00Z",
            },
            "incoming": {"method": "POST", "path": "/v1/messages"},
            "routed": {"url": "http://u"},
            "response": {"status": 200},
        }
        shallow = _shallow_project(entry)
        self.assertIsNone(shallow["duration_ms"])
        self.assertIsNone(shallow["sending_ts"])

    def test_proxy_connection_sending_ts_set_when_routed_before_close(self):
        from journal.store import _shallow_project
        entry = {
            "id": "c-live",
            "type": "proxy_connection",
            "session_id": "s1",
            "timestamps": {
                "received": "2026-05-14T00:00:00Z",
                "routed": "2026-05-14T00:00:02Z",
            },
            "incoming": {"method": "POST", "path": "/v1/messages"},
            "routed": {"url": "http://u"},
            "state": "ROUTING_RESPONSE",
        }
        shallow = _shallow_project(entry)
        self.assertEqual(shallow["sending_ts"], "2026-05-14T00:00:02Z")
        self.assertIsNone(shallow["duration_ms"])

    def test_proxy_connection_duration_computed_with_milliseconds(self):
        from journal.store import _shallow_project
        entry = {
            "id": "c4",
            "type": "proxy_connection",
            "session_id": "s1",
            "timestamps": {
                "received": "2026-05-14T00:00:00.123Z",
                "response_completed": "2026-05-14T00:00:00.456Z",
            },
            "incoming": {"method": "POST", "path": "/v1/messages"},
            "routed": {"url": "http://u"},
            "response": {"status": 200},
        }
        shallow = _shallow_project(entry)
        self.assertEqual(shallow["duration_ms"], 333)

    def test_proxy_connection_duration_fallback_to_classification_scores(self):
        from journal.store import _shallow_project
        entry = {
            "id": "c5",
            "type": "proxy_connection",
            "session_id": "s1",
            "timestamps": {
                "received": "not-a-date",
                "response_completed": "2026-05-14T00:00:01Z",
            },
            "incoming": {"method": "POST", "path": "/v1/messages"},
            "routed": {"url": "http://u"},
            "response": {"status": 200},
            "classification": {
                "scores": {"call_duration_ms": 420.0}
            },
        }
        shallow = _shallow_project(entry)
        # ValueError from unparseable timestamp -> falls back to classification
        self.assertEqual(shallow["duration_ms"], 420.0)

    def test_proxy_connection_duration_none_on_negative_duration(self):
        from journal.store import _shallow_project
        entry = {
            "id": "c6",
            "type": "proxy_connection",
            "session_id": "s1",
            "timestamps": {
                "received": "2026-05-14T00:00:02.000Z",
                "response_completed": "2026-05-14T00:00:01.000Z",
            },
            "incoming": {"method": "POST", "path": "/v1/messages"},
            "routed": {"url": "http://u"},
            "response": {"status": 200},
        }
        shallow = _shallow_project(entry)
        # response_completed before received -> negative diff -> None
        self.assertIsNone(shallow["duration_ms"])


if __name__ == "__main__":
    unittest.main()
