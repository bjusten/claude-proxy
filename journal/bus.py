"""Topic-based pub/sub event bus for the journal subsystem.

Publishers never block: each subscriber owns a bounded deque whose maxlen
handles overflow by evicting the oldest event. Subscriber iterators block
on a per-subscription threading.Event between drains. Exiting the
`subscribe` context manager removes the subscriber from the registry and
signals the iterator to terminate.
"""

import collections
import threading
from contextlib import contextmanager


_DEFAULT_QUEUE_DEPTH = 256


class _Subscription:
    def __init__(self, topics, queue_depth):
        self.topics = tuple(topics)
        self.queue = collections.deque(maxlen=queue_depth)
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.closed = False

    def deliver(self, topic, payload):
        with self.lock:
            self.queue.append((topic, payload))
        self.event.set()

    def close(self):
        self.closed = True
        self.event.set()

    def __iter__(self):
        return self

    def __next__(self):
        while True:
            with self.lock:
                if self.queue:
                    return self.queue.popleft()
            if self.closed:
                raise StopIteration
            self.event.wait(timeout=0.1)
            self.event.clear()


class EventBus:
    def __init__(self):
        self._subs_by_topic = {}
        self._lock = threading.Lock()

    def publish(self, topic, event):
        with self._lock:
            subs = list(self._subs_by_topic.get(topic, ()))
        for sub in subs:
            sub.deliver(topic, event)

    @contextmanager
    def subscribe(self, topics, queue_depth=_DEFAULT_QUEUE_DEPTH):
        sub = _Subscription(topics, queue_depth)
        with self._lock:
            for t in topics:
                self._subs_by_topic.setdefault(t, []).append(sub)
        try:
            yield sub
        finally:
            with self._lock:
                for t in topics:
                    lst = self._subs_by_topic.get(t)
                    if lst is not None and sub in lst:
                        lst.remove(sub)
            sub.close()
