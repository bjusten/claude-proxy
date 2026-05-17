"""Tests for the JournalStore <-> EventBus integration."""

import queue
import threading
import unittest

from journal.bus import EventBus
from journal.store import JournalStore


def _collect(sub, out, max_events):
    n = 0
    for topic, event in sub:
        out.put((topic, event))
        n += 1
        if n >= max_events:
            return


class TestJournalStoreBusAppend(unittest.TestCase):
    def test_append_publishes_shallow_journal_activity_for_proxy_connection(self):
        """proxy_connection append publishes journal_activity for real-time UI update."""
        bus = EventBus()
        store = JournalStore(bus=bus)
        out = queue.Queue()
        ready = threading.Event()

        def worker():
            with bus.subscribe(["journal_activity"]) as sub:
                ready.set()
                _collect(sub, out, 1)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        self.assertTrue(ready.wait(timeout=1.0))

        entry = {
            "id": "abc",
            "type": "proxy_connection",
            "session_id": "s1",
            "timestamps": {"received": "2026-05-14T00:00:00Z"},
            "incoming": {"method": "POST", "path": "/v1/messages",
                         "headers": {}, "body": b""},
            "routed": None,
            "response": None,
            "error": None,
        }
        store.append(entry)

        # append publishes journal_activity so UI updates in real time.
        topic, event = out.get(timeout=1.0)
        self.assertEqual(topic, "journal_activity")
        self.assertEqual(event["id"], "abc")
        self.assertEqual(event["session_id"], "s1")
        t.join(timeout=1.0)


class TestJournalStoreBusUpdate(unittest.TestCase):
    def test_update_publishes_shallow_journal_activity_event(self):
        bus = EventBus()
        store = JournalStore(bus=bus)

        entry = {
            "id": "abc",
            "type": "proxy_connection",
            "session_id": "s1",
            "timestamps": {"received": "2026-05-14T00:00:00Z"},
            "incoming": {"method": "POST", "path": "/v1/messages",
                         "headers": {}, "body": b""},
            "routed": None,
            "response": None,
            "error": None,
        }
        store.append(entry)

        # Subscribe AFTER initial append so we only see the update event.
        out = queue.Queue()
        ready = threading.Event()

        def worker():
            with bus.subscribe(["journal_activity"]) as sub:
                ready.set()
                _collect(sub, out, 1)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        self.assertTrue(ready.wait(timeout=1.0))

        store.update("abc", {"response": {"status": 200, "bytes": 5}})
        topic, event = out.get(timeout=1.0)
        self.assertEqual(topic, "journal_activity")
        self.assertEqual(event["id"], "abc")
        # Post-update shallow projection reflects the new response fields.
        self.assertEqual(event["status"], 200)
        self.assertEqual(event["bytes"], 5)
        t.join(timeout=1.0)


class TestJournalStoreBusNonProxyActivity(unittest.TestCase):
    def test_append_publishes_journal_activity_for_any_entry_type(self):
        """Non-proxy entries (gpu_sample, etc.) also publish journal_activity on append."""
        bus = EventBus()
        store = JournalStore(bus=bus)
        out = queue.Queue()
        ready = threading.Event()
        subscribed = threading.Event()

        def worker():
            with bus.subscribe(["journal_activity"]) as sub:
                subscribed.set()
                _collect(sub, out, 1)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        self.assertTrue(subscribed.wait(timeout=1.0))

        store.append({
            "id": "gpu-1",
            "type": "gpu_sample",
            "ts": "2026-05-14T00:00:00Z",
            "gpus": [{"index": 0}],
        })

        # All entry types now publish journal_activity on append.
        topic, event = out.get(timeout=1.0)
        self.assertEqual(topic, "journal_activity")
        self.assertEqual(event["id"], "gpu-1")
        self.assertEqual(event["type"], "gpu_sample")
        t.join(timeout=1.0)

    def test_update_does_not_publish_journal_activity_for_system_sample(self):
        """system_sample updates should NOT emit journal_activity events."""
        bus = EventBus()
        store = JournalStore(bus=bus)

        entry = {
            "id": "sys-1",
            "type": "system_sample",
            "ts": "2026-05-14T00:00:00Z",
            "uptime_seconds": 100,
        }
        store.append(entry)

        out = queue.Queue()
        ready = threading.Event()
        subscribed = threading.Event()

        def worker():
            with bus.subscribe(["journal_activity"]) as sub:
                subscribed.set()
                _collect(sub, out, 1)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        self.assertTrue(subscribed.wait(timeout=1.0))

        store.update("sys-1", {"memory": {"total_bytes": 16000000000}})

        with self.assertRaises(queue.Empty):
            out.get(timeout=0.5)
        t.join(timeout=1.0)


class TestJournalStoreNoBus(unittest.TestCase):
    def test_store_without_bus_works_normally(self):
        # Back-compat: no bus argument should not crash on append/update.
        store = JournalStore()
        store.append({
            "id": "x",
            "type": "proxy_connection",
            "session_id": "s",
            "timestamps": {"received": "2026-05-14T00:00:00Z"},
            "incoming": {"method": "GET", "path": "/", "headers": {}, "body": b""},
            "routed": None,
            "response": None,
            "error": None,
        })
        self.assertTrue(store.update("x", {"response": {"status": 204, "bytes": 0}}))
        deep = store.get_deep("x")
        self.assertEqual(deep["response"]["status"], 204)
