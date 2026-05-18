"""In-memory activity journal store.

Thread-safe append-only log keyed by entry id. Supports retention via
byte cap, age cap, and background eviction.
"""

import datetime
import json
import threading
import time


def _shallow_project(entry):
    """Project an entry to its shallow summary form."""
    if entry.get("type") == "proxy_connection":
        timestamps = entry.get("timestamps") or {}
        incoming = entry.get("incoming") or {}
        routed = entry.get("routed") or {}
        routing = routed.get("routing") or {}
        classification = entry.get("classification")
        response = entry.get("response") or {}

        # Compute duration_ms from timestamps
        received_ts = timestamps.get("received")
        completed_ts = timestamps.get("response_completed")
        duration_ms = None
        if received_ts and completed_ts:
            try:
                received_dt = datetime.datetime.fromisoformat(received_ts.replace("Z", "+00:00"))
                completed_dt = datetime.datetime.fromisoformat(completed_ts.replace("Z", "+00:00"))
                diff = (completed_dt - received_dt).total_seconds() * 1000
                if diff > 0:
                    duration_ms = round(diff)
            except (ValueError, TypeError):
                duration_ms = None
        # Fall back to classification scores if no timestamp-based duration
        if duration_ms is None:
            duration_ms = (classification or {}).get("scores", {}).get("call_duration_ms")

        return {
            "id": entry.get("id"),
            "type": "proxy_connection",
            "ts": timestamps.get("received"),
            "sending_ts": timestamps.get("routed"),
            "session_id": entry.get("session_id"),
            "method": incoming.get("method"),
            "path": incoming.get("path"),
            "original_model": routed.get("original_model"),
            "new_model": routed.get("new_model"),
            "classified": classification.get("classified") if classification else routing.get("classified"),
            "duration_ms": duration_ms,
            "entry_idx": routing.get("entry_idx"),
            "destination_url": routed.get("url"),
            "status": response.get("status") if response else None,
            "error": entry.get("error"),
            "bytes": response.get("bytes") if response else None,
            "tokens": entry.get("tokens"),
            "state": entry.get("state"),
        }
    return dict(entry)


def _estimate_size(entry):
    """Approximate serialised byte length of an entry.

    Bytes values are not JSON-serialisable; fall back to their length.
    """
    try:
        return len(json.dumps(entry, default=lambda o: len(o) if isinstance(o, (bytes, bytearray)) else str(o)))
    except (TypeError, ValueError):
        return len(repr(entry))


def _deep_merge(target, patch):
    """Recursively merge `patch` into `target` in place."""
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(target.get(k), dict):
            _deep_merge(target[k], v)
        else:
            target[k] = v


class JournalStore:
    def __init__(self, *, max_bytes=1024 ** 3, max_age_seconds=7200,
                 clock=None, autoreap=True, bus=None):
        self._entries = []  # ordered list of full entry dicts
        self._pinned_id = None
        self._lock = threading.Lock()
        self._max_bytes = max_bytes
        self._max_age_seconds = max_age_seconds
        self._clock = clock if clock is not None else time.time
        self._autoreap = autoreap
        self._sizes_by_id = {}        # entry id -> serialised byte length
        self._appended_at = {}        # entry id -> append timestamp (epoch float)
        self._reaper_thread = None
        self._stop_event = threading.Event()
        self._bus = bus

    def append(self, entry, pinned=False):
        """Append a full entry dict; returns the entry id."""
        entry_id = entry["id"]
        with self._lock:
            self._entries.append(entry)
            self._sizes_by_id[entry_id] = _estimate_size(entry)
            self._appended_at[entry_id] = self._clock()
            if pinned:
                self._pinned_id = entry_id
            if self._autoreap:
                self._reap_locked()
            if self._bus is not None:
                self._bus.publish("journal_activity", _shallow_project(entry))
        return entry_id

    def _reap_locked(self):
        """Evict oldest non-pinned entries until both caps are satisfied."""
        # Time-cap first: drop any non-pinned entry older than max_age_seconds.
        now = self._clock()
        cutoff = now - self._max_age_seconds
        survivors = []
        for e in self._entries:
            eid = e["id"]
            if eid != self._pinned_id and self._appended_at.get(eid, now) < cutoff:
                self._sizes_by_id.pop(eid, None)
                self._appended_at.pop(eid, None)
                continue
            survivors.append(e)
        self._entries = survivors

        # Byte-cap: evict oldest non-pinned until total ≤ max_bytes.
        total = sum(self._sizes_by_id.values())
        if total <= self._max_bytes:
            return
        # Iterate from oldest forward, skipping the pinned entry.
        new_entries = list(self._entries)
        i = 0
        while total > self._max_bytes and i < len(new_entries):
            e = new_entries[i]
            eid = e["id"]
            if eid == self._pinned_id:
                i += 1
                continue
            total -= self._sizes_by_id.pop(eid, 0)
            self._appended_at.pop(eid, None)
            new_entries.pop(i)
            # do not increment i; pop shifted the list
        self._entries = new_entries

    def update(self, entry_id, patch):
        """Recursively merge `patch` into the entry; return True if found.

        Dict values merge recursively; non-dict values replace.
        """
        shallow = None
        found = False
        with self._lock:
            for e in self._entries:
                if e["id"] == entry_id:
                    _deep_merge(e, patch)
                    # Refresh cached size to reflect new content.
                    self._sizes_by_id[entry_id] = _estimate_size(e)
                    if self._autoreap:
                        self._reap_locked()
                    if self._bus is not None and e.get("type") == "proxy_connection":
                        shallow = _shallow_project(e)
                    found = True
                    break
        if found and self._bus is not None and shallow is not None:
            self._bus.publish("journal_activity", shallow)
        return found

    def list_shallow(self):
        """Return shallow projections of all retained entries.

        For proxy_launched, the shallow form == the deep form (no headers/bodies
        to strip). For proxy_connection, projects to summary fields only —
        verbatim headers/bodies stay in get_deep.
        """
        with self._lock:
            return [_shallow_project(e) for e in self._entries]

    def get_deep(self, entry_id):
        """Return the full entry by id, or None if not found."""
        with self._lock:
            for e in self._entries:
                if e["id"] == entry_id:
                    return dict(e)
        return None

    def start_eviction_tick(self, interval_seconds=1.0):
        """Start a daemon thread that periodically reaps expired entries.

        Idempotent: a second call while running is a no-op. The thread uses
        the injected clock for age checks and a real Event.wait for pacing.
        """
        if self._reaper_thread is not None and self._reaper_thread.is_alive():
            return self._reaper_thread
        self._stop_event.clear()

        def _loop():
            while not self._stop_event.wait(interval_seconds):
                with self._lock:
                    self._reap_locked()

        t = threading.Thread(target=_loop, name="journal-reaper", daemon=True)
        t.start()
        self._reaper_thread = t
        return t

    def stop_eviction_tick(self, timeout=2.0):
        """Stop the background reaper thread. Safe to call when not started."""
        self._stop_event.set()
        t = self._reaper_thread
        if t is not None:
            t.join(timeout=timeout)
            self._reaper_thread = None

    def pinned_entry(self):
        """Return the entry marked pinned at append time, or None."""
        if self._pinned_id is None:
            return None
        return self.get_deep(self._pinned_id)
