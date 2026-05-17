"""Tests for journal/bus.py — topic-based pub/sub bus."""

import queue
import threading
import unittest

from journal.bus import EventBus


def _drain_into_queue(sub_iter, out_q, max_events=None):
    """Helper: read from a subscription iterator and push onto a queue."""
    count = 0
    for topic, event in sub_iter:
        out_q.put((topic, event))
        count += 1
        if max_events is not None and count >= max_events:
            return


class TestEventBusNoSubscriber(unittest.TestCase):
    def test_publish_with_no_subscriber_is_noop(self):
        bus = EventBus()
        # Should not raise.
        bus.publish("journal_activity", {"a": 1})


class TestEventBusDelivery(unittest.TestCase):
    def test_subscriber_receives_published_event(self):
        bus = EventBus()
        out = queue.Queue()
        ready = threading.Event()

        def worker():
            with bus.subscribe(["journal_activity"]) as sub:
                ready.set()
                _drain_into_queue(sub, out, max_events=1)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        self.assertTrue(ready.wait(timeout=1.0))
        # Give the iterator a moment to enter its wait loop. Publishing should
        # wake it; the deque is filled before publish returns either way.
        bus.publish("journal_activity", {"a": 1})
        topic, event = out.get(timeout=1.0)
        self.assertEqual(topic, "journal_activity")
        self.assertEqual(event, {"a": 1})
        t.join(timeout=1.0)


class TestEventBusTopicFiltering(unittest.TestCase):
    def test_event_on_different_topic_is_not_delivered(self):
        bus = EventBus()
        out = queue.Queue()
        ready = threading.Event()

        def worker():
            with bus.subscribe(["topic_a"]) as sub:
                ready.set()
                _drain_into_queue(sub, out, max_events=1)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        self.assertTrue(ready.wait(timeout=1.0))
        bus.publish("topic_b", {"x": 1})
        with self.assertRaises(queue.Empty):
            out.get(timeout=0.1)
        # cleanup: publish on the subscribed topic so worker thread exits
        bus.publish("topic_a", {"x": 2})
        t.join(timeout=1.0)


class TestEventBusFanout(unittest.TestCase):
    def test_multiple_subscribers_receive_same_event(self):
        bus = EventBus()
        out_a = queue.Queue()
        out_b = queue.Queue()
        ready_a = threading.Event()
        ready_b = threading.Event()

        def worker(out, ready):
            with bus.subscribe(["journal_activity"]) as sub:
                ready.set()
                _drain_into_queue(sub, out, max_events=1)

        ta = threading.Thread(target=worker, args=(out_a, ready_a), daemon=True)
        tb = threading.Thread(target=worker, args=(out_b, ready_b), daemon=True)
        ta.start()
        tb.start()
        self.assertTrue(ready_a.wait(timeout=1.0))
        self.assertTrue(ready_b.wait(timeout=1.0))
        bus.publish("journal_activity", {"k": "v"})
        self.assertEqual(out_a.get(timeout=1.0), ("journal_activity", {"k": "v"}))
        self.assertEqual(out_b.get(timeout=1.0), ("journal_activity", {"k": "v"}))
        ta.join(timeout=1.0)
        tb.join(timeout=1.0)


class TestEventBusMultiTopic(unittest.TestCase):
    def test_subscribe_to_multiple_topics_yields_both_in_order(self):
        bus = EventBus()
        out = queue.Queue()
        ready = threading.Event()

        def worker():
            with bus.subscribe(["a", "b"]) as sub:
                ready.set()
                _drain_into_queue(sub, out, max_events=2)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        self.assertTrue(ready.wait(timeout=1.0))
        bus.publish("a", {"n": 1})
        bus.publish("b", {"n": 2})
        first = out.get(timeout=1.0)
        second = out.get(timeout=1.0)
        self.assertEqual(first, ("a", {"n": 1}))
        self.assertEqual(second, ("b", {"n": 2}))
        t.join(timeout=1.0)


class TestEventBusBackpressure(unittest.TestCase):
    def test_overflow_drops_oldest_events(self):
        bus = EventBus()
        # Subscribe and immediately drain off the main thread so we can publish
        # 6 events before any reader pulls; depth=4 should keep only the LAST
        # four.
        with bus.subscribe(["t"], queue_depth=4) as sub:
            for i in range(6):
                bus.publish("t", {"i": i})
            # Pull all available events from the deque without blocking on
            # future ones. Because the bus's iterator parks on event.wait when
            # the deque is empty, we read directly off the subscription's
            # underlying queue.
            with sub.lock:
                pending = list(sub.queue)
        self.assertEqual([p[1]["i"] for p in pending], [2, 3, 4, 5])

    def test_publisher_never_blocks_under_backpressure(self):
        import time
        bus = EventBus()
        with bus.subscribe(["t"], queue_depth=4):
            start = time.monotonic()
            for i in range(1000):
                bus.publish("t", {"i": i})
            elapsed = time.monotonic() - start
        # generous threshold: 1000 publishes should be well under 1s.
        self.assertLess(elapsed, 1.0)


class TestEventBusCleanup(unittest.TestCase):
    def test_context_manager_removes_subscriber_on_exit(self):
        bus = EventBus()
        with bus.subscribe(["t"]) as sub:
            pass
        # After exit, the bus must no longer reference the subscription on
        # topic "t". Publishing afterwards must not append to the closed sub.
        registry = bus._subs_by_topic.get("t", [])
        self.assertNotIn(sub, registry)
        bus.publish("t", {"i": 1})
        with sub.lock:
            self.assertEqual(len(sub.queue), 0)

    def test_iterator_terminates_when_context_exits(self):
        bus = EventBus()
        done = threading.Event()
        sub_holder = {}
        enter = threading.Event()

        def worker():
            with bus.subscribe(["t"]) as sub:
                sub_holder["sub"] = sub
                enter.set()
                # Drain until close. Iterator must raise StopIteration after
                # the with-block exits.
                for _ in sub:
                    pass
            done.set()

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        self.assertTrue(enter.wait(timeout=1.0))
        # The with-block exits only when the iterator returns, but the
        # iterator returns only when sub is closed. Resolve the deadlock
        # by closing the sub externally — this simulates a sibling shutting
        # things down. In real usage the context exits because the consumer
        # returns from its loop (e.g. broken pipe), and __exit__ then closes
        # the sub so any internal threads can break. Here we exercise close().
        sub_holder["sub"].close()
        self.assertTrue(done.wait(timeout=1.0))
        t.join(timeout=1.0)
