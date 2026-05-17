#!/usr/bin/env python3
"""
Enhanced Claude Code API Proxy
- Routes requests based on model
- Supports config entries: { urls: [{url, new_model_name}...] }
- Least-active-connections load balancing with tie-break by array order
- Rewrites request JSON body to swap model name
- Writes per-request session log (claude-proxy-sessions.log)
"""

import json
import os
import sys
import argparse
import logging
import signal
import shutil
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import threading
import urllib.request
from urllib.error import URLError, HTTPError
from urllib.parse import urlparse

import classifier
from journal import api as journal_api
from journal import config as journal_config
from journal import correlator as journal_correlator
from journal import ui_server as journal_ui_server
from journal import usage as journal_usage
from journal.auth import AuthStore
from journal.bus import EventBus
from journal.pollers import (
    NvidiaSmiPoller,
    SystemInfoPoller,
    default_nvidia_smi_sampler,
    make_default_system_sampler,
)
from journal.store import JournalStore
import datetime
import uuid


def _now_iso():
    return datetime.datetime.now(datetime.UTC).isoformat()


def _ago_iso(ms):
    """Return an ISO-8601 timestamp `ms` milliseconds in the past."""
    return (datetime.datetime.now(datetime.UTC) - datetime.timedelta(milliseconds=ms)).isoformat(timespec="milliseconds")


# Lifecycle state constants for proxy_connection journal entries
STATE_INIT = "INIT"
STATE_CLASSIFYING = "CLASSIFYING"
STATE_QUEUED = "QUEUED"
# ROUTING is split into two phases so the UI can distinguish "sending the
# proxied request" (blue) from "request sent, awaiting the upstream
# response" (yellow). ROUTING_REQUEST is set once the destination is
# resolved and the request is being built/dispatched; ROUTING_RESPONSE is
# set immediately before we block on the upstream response.
STATE_ROUTING_REQUEST = "ROUTING_REQUEST"
STATE_ROUTING_RESPONSE = "ROUTING_RESPONSE"
STATE_SUCCESS = "SUCCESS"
STATE_FAILURE = "FAILURE"


# ANSI escape codes for terminal colors
_ANSI_COLORS = [
    "\033[0;31m",  # Red
    "\033[0;32m",  # Green
    "\033[0;33m",  # Yellow
    "\033[0;34m",  # Blue
    "\033[0;35m",  # Magenta
    "\033[0;36m",  # Cyan
    "\033[0;91m",  # Bright Red
    "\033[0;92m",  # Bright Green
    "\033[0;93m",  # Bright Yellow
    "\033[0;94m",  # Bright Blue
    "\033[0;95m",  # Bright Magenta
    "\033[0;96m",  # Bright Cyan
]
_ANSI_RESET = "\033[0m"


def default_proxy_settings() -> dict:
    """Default proxy settings, written to proxy.json when it is missing."""
    return {
        "upstream_timeout_seconds": 300,
        "global_url_tracking": False,
        "wait_timeout_seconds": 300,
        "enable_session_logging": False,
    }


def default_models() -> dict:
    """Example routing map, written to models.json when it is missing.

    The `1.2.3.4` backends are illustrative — edit them for your hosts.
    """
    return {
        "model-a": {
            "urls": [
                {"url": "http://1.2.3.4:5678", "new_model_name": "llama3:70b"},
                {"url": "http://1.2.3.4:5679", "new_model_name": "llama3:8b"},
            ]
        },
        "model-b": {
            "urls": [
                {"url": "http://1.2.3.4:5678", "new_model_name": "mistral:7b"},
            ]
        },
    }


def build_url_colors(models: dict) -> dict:
    """Map each distinct upstream URL in *models* to a stable ANSI color."""
    url_colors: dict[str, str] = {}
    url_index = 0
    for entry in models.values():
        if not isinstance(entry, dict):
            continue
        for url_entry in entry.get("urls", []):
            url = url_entry["url"]
            if url not in url_colors:
                url_colors[url] = _ANSI_COLORS[url_index % len(_ANSI_COLORS)]
                url_index += 1
    return url_colors


class ColorHandler(logging.Handler):
    """Logging handler that wraps entire log lines in ANSI colors."""

    def __init__(self, url_colors, level=logging.NOTSET):
        super().__init__(level)
        self.url_colors = url_colors

    def emit(self, record):
        msg = self.format(record)
        color = ""
        if hasattr(record, "url_color") and record.url_color:
            color = record.url_color
        try:
            stream = sys.stdout
            stream.write(color + msg + _ANSI_RESET)
            stream.write(os.linesep)
            stream.flush()
        except Exception:
            self.handleError(record)

logger = logging.getLogger("claude-proxy")
logger.setLevel(logging.INFO)

# File logger for per-session entries
_session_logger = logging.getLogger("claude-proxy.sessions")
_session_logger.setLevel(logging.INFO)
_fh = logging.FileHandler(os.path.join(os.getcwd(), "claude-proxy-sessions.log"))
_fh.setFormatter(logging.Formatter("%(message)s"))
_session_logger.addHandler(_fh)


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class LeastActiveSelector:
    """Least-active-connections selector with tie-breaking by array order.

    Each entry has an active connection counter (no per-entry lock).
    New connections pick the entry with the lowest active count;
    ties are broken by earliest array index.
    """

    def __init__(self):
        self._active: dict[str, list[int]] = {}
        self._lock = threading.Lock()

    def acquire(self, key: str, entries: list, global_tracking: bool = False,
                wait_timeout: float = 0, on_queue=None) -> int:
        if global_tracking and wait_timeout > 0:
            return self._acquire_global(key, entries, wait_timeout, on_queue)
        with self._lock:
            if key not in self._active:
                self._active[key] = [0] * len(entries)
            if global_tracking:
                # Find entry with minimum global active connections; tie-break by index
                global_counts = [_url_tracker.get_active(e.get("url", e)) for e in entries]
                idx = min(range(len(entries)), key=lambda i: (global_counts[i], i))
                _url_tracker.acquire(entries[idx].get("url", entries[idx]))
            else:
                # Find entry with minimum active connections; tie-break by index
                idx = min(range(len(entries)), key=lambda i: self._active[key][i])
                self._active[key][idx] += 1
            return idx

    def _acquire_global(self, key: str, entries: list, wait_timeout: float, on_queue=None) -> int:
        """Acquire entry with global URL tracking, waiting for a free URL if needed."""
        signalled = False
        while True:
            with self._lock:
                if key not in self._active:
                    self._active[key] = [0] * len(entries)
                global_counts = [_url_tracker.get_active(e.get("url", e)) for e in entries]
                if any(c == 0 for c in global_counts):
                    # At least one URL is free — pick the minimum
                    idx = min(range(len(entries)), key=lambda i: (global_counts[i], i))
                    _url_tracker.acquire(entries[idx].get("url", entries[idx]))
                    return idx
            # All URLs are busy — release the lock and wait for a slot
            if on_queue is not None and not signalled:
                on_queue()
                signalled = True
            if not _url_tracker.wait_for_free_url(wait_timeout):
                # Timeout expired — fall through to pick the minimum anyway
                with self._lock:
                    global_counts = [_url_tracker.get_active(e.get("url", e)) for e in entries]
                    idx = min(range(len(entries)), key=lambda i: (global_counts[i], i))
                    _url_tracker.acquire(entries[idx].get("url", entries[idx]))
                    return idx

    def release(self, key: str, idx: int, url: str | None = None) -> None:
        with self._lock:
            if key in self._active:
                self._active[key][idx] -= 1
        if url is not None:
            _url_tracker.release(url)


_rr = LeastActiveSelector()


class _UrlTracker:
    """Global per-host:port active connection tracker.

    When global_url_tracking is enabled, this ensures the same
    upstream host:port is never double-acquired across routing keys.
    """

    def __init__(self):
        self._count: dict[str, int] = {}
        self._lock = threading.Lock()

    def any_free(self) -> bool:
        """Return True if any tracked URL has zero active connections."""
        with self._lock:
            return any(c == 0 for c in self._count.values())

    def wait_for_free_url(self, timeout: float) -> bool:
        """Block until any tracked URL becomes free, or timeout expires.

        Returns True if a URL became available, False if the timeout expired.
        """
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.any_free():
                return True
            time.sleep(0.1)
        return False

    def acquire(self, url: str) -> None:
        with self._lock:
            key = urlparse(url).netloc
            self._count[key] = self._count.get(key, 0) + 1

    def release(self, url: str) -> None:
        with self._lock:
            key = urlparse(url).netloc
            current = self._count.get(key, 0)
            if current > 1:
                self._count[key] = current - 1
            else:
                self._count.pop(key, None)

    def get_active(self, url: str) -> int:
        with self._lock:
            return self._count.get(urlparse(url).netloc, 0)


_url_tracker = _UrlTracker()


class ActiveSessionCounter:
    """Thread-safe counter of currently active (in-flight) connections."""

    def __init__(self):
        self._count = 0
        self._lock = threading.Lock()

    def inc(self) -> int:
        with self._lock:
            self._count += 1
            return self._count

    def dec(self) -> int:
        with self._lock:
            self._count -= 1
            return self._count


_active_sessions = ActiveSessionCounter()


class ClaudeProxyHandler(BaseHTTPRequestHandler):
    JOURNAL_PREFIX = "/__journal/"
    UI_PREFIX = "/__ui/"

    def __init__(self, *args, models=None, proxy_settings=None, url_colors=None, classification_config=None, journal_config=None, journal_store=None, journal_bus=None, gpu_poller=None, system_poller=None, auth_store=None, ui_theme="system", ui_show_gpu=False, ui_show_system=False, ui_enable_gpu=True, ui_enable_system=True, ui_state_colors=None, **kwargs):
        self.models = models or {}
        self.proxy_settings = proxy_settings or {}
        self._url_colors = url_colors or {}
        self.classification_config = classification_config or {"enabled": False}
        self.journal_config = journal_config or {}
        self.journal_store = journal_store
        self.journal_bus = journal_bus
        self.gpu_poller = gpu_poller
        self.system_poller = system_poller
        self.auth_store = auth_store
        self.ui_theme = ui_theme
        self.ui_show_gpu = ui_show_gpu
        self.ui_show_system = ui_show_system
        self.ui_enable_gpu = ui_enable_gpu
        self.ui_enable_system = ui_enable_system
        self.ui_state_colors = ui_state_colors or {}
        super().__init__(*args, **kwargs)

    def _set_state(self, entry_id, state):
        """Update the lifecycle state of a journal entry."""
        if entry_id is not None and self.journal_store is not None:
            self.journal_store.update(entry_id, {"state": state})

    # ------------------------------------------------------------------
    # Redaction helpers
    # ------------------------------------------------------------------

    @property
    def _redact_fields(self):
        """Configurable set of field names to redact from request/response bodies."""
        return set(
            self.journal_config.get("redact_fields", [
                "system",
                "api_key",
                "Authorization",
                "api_secret",
                "private_key",
            ])
        )

    @property
    def _redact_max_body_bytes(self):
        """Maximum body size before truncation to prevent journal bloat."""
        return int(self.journal_config.get("max_body_bytes", 10240))

    @property
    def _redact_max_content_bytes(self):
        """Maximum length for individual content strings in redacted bodies."""
        return int(self.journal_config.get("max_body_bytes", 10240))

    @property
    def _upstream_timeout(self):
        """Socket timeout (seconds) for the forwarded upstream request.

        Applies per blocking socket operation, so streaming responses reset
        it on every chunk received. ``null`` in config disables the timeout
        entirely (block until the model responds).
        """
        val = self.proxy_settings.get("upstream_timeout_seconds", 300)
        return None if val is None else float(val)

    def _redact_body(self, body_bytes):
        """Return a redacted copy of *body_bytes*.

        When the body parses as JSON, sensitive fields are removed and
        very large content is truncated.  Returns the redacted body as
        ``bytes`` so journal entries remain unchanged in type.
        """
        if not body_bytes:
            return body_bytes

        # Limit body size upfront to bound work on pathological payloads
        if len(body_bytes) > self._redact_max_body_bytes * 4:
            return body_bytes[: self._redact_max_body_bytes] + b" [... truncated]"

        try:
            data = json.loads(body_bytes)
        except (json.JSONDecodeError, ValueError):
            # Not JSON -- truncate if too large
            if len(body_bytes) > self._redact_max_body_bytes:
                return body_bytes[: self._redact_max_body_bytes] + b" [... truncated]"
            return body_bytes

        data = _redact_json(data, self._redact_fields, self._redact_max_content_bytes)

        return json.dumps(data, default=str).encode("utf-8")

    def log_proxy_request(self, original_model, destination_url, new_model, url_color=""):
        color = self._url_colors.get(destination_url, "")
        cls = getattr(self, "_classified", None)
        scores = getattr(self, "_class_scores", None)
        if cls and scores:
            logger.info(
                "%s %s -> model='%s' classified='%s' (code=%.1f reasoning=%.1f) rewritten='%s' -> destination='%s'",
                self.command, self.path, original_model, cls,
                scores["code"], scores["reasoning"], new_model, destination_url,
                extra={"url_color": color},
            )
        else:
            logger.info(
                "%s %s -> model='%s' rewritten='%s' -> destination='%s'",
                self.command, self.path, original_model, new_model, destination_url,
                extra={"url_color": color},
            )

    def _resolve_destination(self, original_model: str, on_queue=None):
        """Look up config and return (destination_url, new_model, entry_index).

        When there are multiple entries, the entry with the fewest active
        connections is chosen (tie-break by array order). The caller must
        release the entry after the request completes.
        """
        global_tracking = self.proxy_settings.get("global_url_tracking", False)
        wait_timeout = self.proxy_settings.get("wait_timeout_seconds", 300)
        config_entry = self.models.get(original_model) or self.models.get("default")
        if not config_entry:
            return "http://127.0.0.1:11434", "qwen3.6:27b", -1

        urls = config_entry.get("urls", [])
        if not urls:
            return config_entry.get("url", "http://127.0.0.1:11434"), config_entry.get("new_model_name", original_model), -1

        if len(urls) == 1:
            entry = urls[0]
            return entry["url"], entry.get("new_model_name", original_model), -1

        idx = _rr.acquire(original_model, urls, global_tracking, wait_timeout, on_queue=on_queue)
        entry = urls[idx]
        return entry["url"], entry.get("new_model_name", original_model), idx

    def _classify_or_passthrough(self, request_data):
        """Return the routing key.

        When classification is enabled, run the classifier and return its result
        (stashing scores on the instance for later logging). Otherwise return
        the original `model` field from the request body.
        """
        original = request_data.get("model", "") if isinstance(request_data, dict) else ""
        self._classified = None
        self._class_scores = None
        self._llm_exchange = None
        if not self.classification_config.get("enabled"):
            return original
        try:
            cls, scores = classifier.classify_request(request_data or {}, self.classification_config)
        except Exception as e:
            logger.warning("Classifier failed, falling back to original model: %s", e)
            return original
        self._classified = cls
        scores = scores or {}
        # Keep the verbatim LLM transcript out of the proxy_connection routing
        # scores; it gets its own journal entry instead.
        self._llm_exchange = scores.pop("llm_exchange", None)
        self._class_scores = scores
        warn_ms = self.classification_config.get("budget_warn_ms", 1000)
        if scores["scan_ms"] > warn_ms:
            logger.warning(
                "Classifier exceeded budget: %.1fms > %dms", scores["scan_ms"], warn_ms
            )
        return cls

    def _journal_classification_exchange(self):
        """Append the LLM classification call as its own journal entry.

        All classification calls share the synthetic session id
        "classification requests" so the UI groups them under a single
        reviewable row, separate from the user's proxied traffic. No-op for
        the heuristic path (no `_llm_exchange` recorded).
        """
        ex = getattr(self, "_llm_exchange", None)
        if not ex or self.journal_store is None:
            return
        response_text = ex.get("response") or ""
        llm_url = ex.get("url") or ""
        llm_model = ex.get("model") or ""
        llm_input = ex.get("input") or ""
        # Reconstruct the actual system prompt that was sent to the LLM
        allowed_words = self.classification_config.get("_llm_words", [])
        allowed_str = ", ".join(allowed_words) if allowed_words else "[none]"
        system_content = (
            "You are a classification model. Given a user request, classify it into exactly ONE category.\n\n"
            "Allowed categories: " + allowed_str + ".\n\n"
            "Rules:\n"
            "- Respond with exactly ONE category word from the allowed list.\n"
            "- No punctuation, no explanation, no quotes.\n"
            "- If the input is too ambiguous, garbled, or you genuinely cannot determine a category, respond with:\n"
            "  ERROR: <brief reason why you cannot classify>\n"
        )
        # Prefer the actual payload from the exchange (carries exact text sent)
        actual_payload = ex.get("payload")
        # Real duration from the exchange (call_duration_ms for LLM path,
        # duration_ms/scan_ms for heuristic fallback on error).
        call_duration_ms = ex.get("call_duration_ms")
        total_duration_ms = call_duration_ms or ex.get("duration_ms") or 0.0
        # Reconstruct a plausible timeline from the actual duration.
        # The entry is created post-hoc (classification already finished),
        # so back-calculate a received timestamp from the total duration.
        now = _now_iso()
        if total_duration_ms and total_duration_ms > 0:
            received = _ago_iso(total_duration_ms)
            response_started = _ago_iso(max(1, total_duration_ms * 0.1))
            response_completed = _now_iso()
        else:
            received = now
            response_started = now
            response_completed = now
        _cls_status = ex.get("status")
        _cls_state = (
            STATE_FAILURE
            if (ex.get("error") or _cls_status is None or not (200 <= _cls_status < 400))
            else STATE_SUCCESS
        )
        self.journal_store.append({
            "id": uuid.uuid4().hex,
            "type": "proxy_connection",
            "session_id": "classification requests",
            "timestamps": {
                "received": received,
                "response_started": response_started,
                "response_completed": response_completed,
            },
            "state": _cls_state,
            "incoming": {
                "method": "POST",
                "path": urlparse(llm_url).path or "/",
                "headers": {},
                "body": llm_input,
            },
            "request": {
                "url": llm_url,
                "model": llm_model,
                "headers": {"Content-Type": "application/json"},
                "body": actual_payload or {
                    "model": llm_model,
                    "messages": [
                        {"role": "system", "content": system_content},
                        {"role": "user", "content": llm_input},
                    ],
                    "temperature": 0,
                    "max_tokens": self.classification_config.get("llm_max_tokens", 1024),
                    "stream": False,
                },
            },
            "classification": {
                "classified": ex.get("classified"),
                "raw": ex.get("raw_class"),
                "scores": {
                    "duration_ms": total_duration_ms,
                    "call_duration_ms": call_duration_ms,
                    "fallback": ex.get("fallback"),
                },
            },
            "response": {
                "status": ex.get("status"),
                "headers": {},
                "body": response_text,
                "bytes": len(response_text.encode("utf-8")),
            },
            "error": (
                {"kind": "ClassificationError", "message": ex["error"]}
                if ex.get("error") else None
            ),
            "tokens": journal_usage.extract_usage(
                response_text.encode("utf-8"), "application/json",
            ),
        })

    def handle_proxy_request(self, expect_body=True):
        # UI short-circuits before any other routing (only when UI is enabled,
        # signalled by an injected AuthStore). When disabled, `/__ui/*` falls
        # through to upstream — parity with `/__journal/*` when journaling is off.
        if self.auth_store is not None and self.path.startswith(self.UI_PREFIX):
            self._handle_ui()
            return

        # Journal queries short-circuit before any upstream routing.
        if self.journal_store is not None and self.path.startswith(self.JOURNAL_PREFIX):
            self._handle_journal()
            return

        # Only proxy OpenAI-style paths (/v1/*). Everything else is rejected.
        if not self.path.startswith("/v1/"):
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > 0:
                self.rfile.read(content_length)
            self.send_error(404, "Not Found")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        post_data = (
            self.rfile.read(content_length)
            if expect_body and content_length > 0
            else b""
        )

        # Parse incoming JSON
        try:
            request_data = json.loads(post_data) if post_data else {}
        except json.JSONDecodeError:
            request_data = {}

        # Build initial proxy_connection journal entry (if journaling enabled)
        journal_entry_id = None
        if self.journal_store is not None:
            # Redact sensitive fields before storing in journal
            post_data = self._redact_body(post_data)
            incoming_headers = {k: v for k, v in self.headers.items()}
            peer = self.client_address[0] if self.client_address else ""
            session_id = journal_correlator.session_id_for(
                incoming_headers, peer, request_data
            )
            journal_entry_id = uuid.uuid4().hex
            # Redact sensitive fields from incoming body before journaling
            post_data = self._redact_body(post_data)
            self.journal_store.append({
                "id": journal_entry_id,
                "type": "proxy_connection",
                "session_id": session_id,
                "timestamps": {"received": _now_iso()},
                "state": STATE_INIT,
                "incoming": {
                    "method": self.command,
                    "path": self.path,
                    "headers": incoming_headers,
                    "body": post_data,
                },
                "routed": None,
                "response": None,
                "error": None,
            })
            post_data = b""  # invalidate original body so we rewrite with new model
        else:
            post_data = b""  # invalidate raw body so rewrite block builds fresh one

        original_model = request_data.get("model", "") if request_data else ""
        classified_before = getattr(self, "_classified", None)
        if journal_entry_id is not None and self.classification_config.get("enabled"):
            self._set_state(journal_entry_id, STATE_CLASSIFYING)
        routing_key = self._classify_or_passthrough(request_data)
        if journal_entry_id is not None:
            patch = {"timestamps": {}}
            cls = getattr(self, "_classified", None)
            scores = getattr(self, "_class_scores", None)
            if cls is not None or scores is not None:
                patch["timestamps"]["classified"] = _now_iso()
            self.journal_store.update(journal_entry_id, patch)
            self._journal_classification_exchange()

        on_queue = (lambda: self._set_state(journal_entry_id, STATE_QUEUED)) if journal_entry_id is not None else None
        destination_url, new_model, entry_idx = self._resolve_destination(routing_key, on_queue=on_queue)
        if journal_entry_id is not None:
            self._set_state(journal_entry_id, STATE_ROUTING_REQUEST)

        # Log connection acquired
        _active_sessions.inc()
        if self.proxy_settings.get("enable_session_logging", False):
            cls = getattr(self, "_classified", None)
            scores = getattr(self, "_class_scores", None)
            if cls and scores:
                _session_logger.info(
                    "ACQUIRED active=%d model='%s' classified='%s' "
                    "scores='code=%.1f,reasoning=%.1f,scan_ms=%.1f' idx=%d url='%s' rewritten='%s'",
                    _active_sessions._count, original_model, cls,
                    scores["code"], scores["reasoning"], scores["scan_ms"],
                    entry_idx, destination_url, new_model,
                )
            else:
                _session_logger.info(
                    "ACQUIRED active=%d model='%s' idx=%d url='%s' rewritten='%s'",
                    _active_sessions._count, original_model, entry_idx, destination_url, new_model,
                )

        # Rewrite request body
        if request_data:
            request_data["model"] = new_model
            post_data = json.dumps(request_data).encode("utf-8")
        elif not post_data and self.command != "HEAD":
            # No body — send minimal request with new model
            post_data = json.dumps({"model": new_model}).encode("utf-8")

        full_url = destination_url.rstrip("/") + self.path

        self.log_proxy_request(original_model, destination_url, new_model, getattr(self, "_url_color", ""))

        excluded_headers = {
            "content-length",
            "host",
            "connection",
            "transfer-encoding",
        }

        req = urllib.request.Request(
            full_url,
            data=post_data if post_data else None,
            method=self.command,
        )

        for header, value in self.headers.items():
            if header.lower() not in excluded_headers:
                req.add_header(header, value)

        upstream_host = urlparse(full_url).netloc
        req.add_header("Host", upstream_host)

        if post_data:
            req.add_header("Content-Length", str(len(post_data)))

        # Capture routed envelope (after request object is built so headers reflect what we send)
        if journal_entry_id is not None:
            cls = getattr(self, "_classified", None)
            scores = getattr(self, "_class_scores", None)
            routing = {
                "mode": "classified" if cls is not None else "model",
                "entry_idx": entry_idx,
            }
            if cls is not None:
                routing["classified"] = cls
            if scores is not None:
                routing["scores"] = scores
            # Redact sensitive fields before storing in journal
            routed_body = self._redact_body(post_data)
            self.journal_store.update(journal_entry_id, {
                "timestamps": {"routed": _now_iso()},
                "routed": {
                    "url": destination_url,
                    "headers": {h: v for h, v in req.header_items()},
                    "body": routed_body,
                    "original_model": original_model,
                    "new_model": new_model,
                    "routing": routing,
                },
            })

        if journal_entry_id is not None:
            self._set_state(journal_entry_id, STATE_ROUTING_RESPONSE)

        try:
            with urllib.request.urlopen(req, timeout=self._upstream_timeout) as response:
                if journal_entry_id is not None:
                    self.journal_store.update(journal_entry_id, {
                        "timestamps": {"response_started": _now_iso()},
                    })
                self.send_response(response.getcode())

                resp_headers = []
                for header, value in response.getheaders():
                    resp_headers.append((header, value))
                    if header.lower() not in excluded_headers:
                        self.send_header(header, value)

                self.end_headers()

                resp_body = b""
                if self.command != "HEAD":
                    # Stream upstream → client in bounded chunks so a slow but
                    # progressing model delivers data incrementally and the
                    # per-read socket timeout resets on every chunk. The body
                    # is also accumulated for journaling/usage extraction.
                    chunks = []
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        chunks.append(chunk)
                    resp_body = b"".join(chunks)

                if journal_entry_id is not None:
                    resp_content_type = ""
                    for h, v in resp_headers:
                        if h.lower() == "content-type":
                            resp_content_type = v
                            break
                    tokens = journal_usage.extract_usage(resp_body, resp_content_type)
                    # Redact sensitive fields before storing in journal
                    resp_body_redacted = self._redact_body(resp_body)
                    patch = {
                        "timestamps": {"response_completed": _now_iso()},
                        "response": {
                            "status": response.getcode(),
                            "headers": {h: v for h, v in resp_headers},
                            "body": resp_body_redacted,
                            "bytes": len(resp_body),
                        },
                    }
                    if tokens is not None:
                        patch["tokens"] = tokens
                    self.journal_store.update(journal_entry_id, patch)
                if journal_entry_id is not None:
                    code = response.getcode()
                    self._set_state(journal_entry_id, STATE_SUCCESS if 200 <= code < 400 else STATE_FAILURE)

        except HTTPError as e:
            logger.error("Upstream HTTP error: %s %s", e.code, e.reason)
            self.send_response(e.code)
            for header, value in e.headers.items():
                if header.lower() not in excluded_headers:
                    self.send_header(header, value)
            self.end_headers()
            err_body = b""
            if self.command != "HEAD" and e.fp:
                err_body = e.fp.read()
                self.wfile.write(err_body)
            if journal_entry_id is not None:
                err_content_type = e.headers.get("Content-Type", "") if e.headers else ""
                tokens = journal_usage.extract_usage(err_body, err_content_type)
                # Redact sensitive fields before storing in journal
                err_body_redacted = self._redact_body(err_body)
                patch = {
                    "timestamps": {"response_completed": _now_iso()},
                    "response": {
                        "status": e.code,
                        "headers": {h: v for h, v in e.headers.items()},
                        "body": err_body_redacted,
                        "bytes": len(err_body),
                    },
                    "error": {"kind": "HTTPError", "message": f"{e.code} {e.reason}"},
                }
                if tokens is not None:
                    patch["tokens"] = tokens
                self.journal_store.update(journal_entry_id, patch)
                self._set_state(journal_entry_id, STATE_FAILURE)

        except URLError as e:
            logger.error("Failed to connect to upstream %s: %s", destination_url, e.reason)
            self.send_error(502, f"Failed to connect to upstream: {e.reason}")
            if journal_entry_id is not None:
                self.journal_store.update(journal_entry_id, {
                    "timestamps": {"response_completed": _now_iso()},
                    "error": {"kind": "URLError", "message": str(e.reason)},
                })
                self._set_state(journal_entry_id, STATE_FAILURE)

        except Exception as e:
            logger.error("Failed to forward request to %s: %s", destination_url, e)
            self.send_error(500, f"Failed to forward request: {e}")
            if journal_entry_id is not None:
                self.journal_store.update(journal_entry_id, {
                    "timestamps": {"response_completed": _now_iso()},
                    "error": {"kind": type(e).__name__, "message": str(e)},
                })
                self._set_state(journal_entry_id, STATE_FAILURE)

        finally:
            if entry_idx >= 0:
                _rr.release(routing_key, entry_idx)
                if self.proxy_settings.get("global_url_tracking", False):
                    _url_tracker.release(destination_url)
            _active_sessions.dec()
            if self.proxy_settings.get("enable_session_logging", False):
                _session_logger.info(
                    "RELEASED active=%d model='%s' idx=%d url='%s' rewritten='%s'",
                    _active_sessions._count, original_model, entry_idx, destination_url, new_model,
                )

    def _handle_journal(self):
        # When the UI is enabled, `/__journal/*` (including the SSE stream
        # handshake) requires a valid session cookie. SSE auth happens once
        # at handshake; the stream itself isn't re-validated mid-flight.
        if self.auth_store is not None:
            token = journal_ui_server.extract_session_cookie(
                {k: v for k, v in self.headers.items()}
            )
            if not token or not self.auth_store.validate(token):
                self._unauthorized()
                return

        # Stream endpoint: /__journal/stream?topic=...&topic=...
        if self.path.startswith("/__journal/stream"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query, keep_blank_values=False)
            topics = qs.get("topic", [])
            bus = getattr(self, "journal_bus", None)
            journal_api.stream(self, self.journal_store, bus, topics)
            return

        gpu_provider = None
        if self.gpu_poller is not None:
            gpu_provider = lambda: self.gpu_poller.latest
        system_provider = None
        if self.system_poller is not None:
            system_provider = lambda: self.system_poller.latest
        status, headers, body = journal_api.dispatch(
            self.command, self.path, self.journal_store,
            gpu_provider=gpu_provider, system_provider=system_provider,
        )
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _handle_ui(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""
        headers = {k: v for k, v in self.headers.items()}
        status, resp_headers, resp_body = journal_ui_server.dispatch_ui(
            self.command, self.path, headers, body, self.auth_store,
            ui_theme=self.ui_theme,
            classification_enabled=bool(self.classification_config.get("enabled")),
            ui_show_gpu=self.ui_show_gpu,
            ui_show_system=self.ui_show_system,
            ui_enable_gpu=self.ui_enable_gpu,
            ui_enable_system=self.ui_enable_system,
            ui_state_colors=self.ui_state_colors,
        )
        self.send_response(status)
        for k, v in resp_headers.items():
            self.send_header(k, v)
        if "Content-Length" not in resp_headers:
            self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(resp_body)

    def _unauthorized(self):
        body = b'{"error": "unauthorized"}'
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.handle_proxy_request(expect_body=False)

    def do_POST(self):
        self.handle_proxy_request(expect_body=True)

    def do_GET(self):
        self.handle_proxy_request(expect_body=False)


def make_proxy_launched_entry(port):
    """Build the pinned proxy_launched journal entry."""
    return {
        "id": uuid.uuid4().hex,
        "type": "proxy_launched",
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "pid": os.getpid(),
        "port": port,
    }


def main():
    parser = argparse.ArgumentParser(description="Claude Code API Proxy")
    parser.add_argument("--port", "-p", type=int, default=8008, help="Port to listen on")
    parser.add_argument(
        "--proxy-config",
        default="proxy.json",
        help="Path to proxy settings file",
    )
    parser.add_argument(
        "--models-config",
        default="models.json",
        help="Path to model routing file",
    )
    parser.add_argument("--color", action="store_true", help="Colorize log output per upstream URL")
    parser.add_argument(
        "--classification-config",
        default="classification.json",
        help="Path to classification config file",
    )
    parser.add_argument(
        "--journal-config",
        default="journal.json",
        help="Path to journal config file",
    )

    args = parser.parse_args()

    def _load_json_config(arg_path: str, default_factory, label: str) -> dict:
        """Resolve, auto-create-if-missing, and load a JSON config file."""
        if os.path.isabs(arg_path):
            path = arg_path
        else:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), arg_path)

        if not os.path.exists(path):
            with open(path, "w") as f:
                json.dump(default_factory(), f, indent=2)
            print(f"Created default {label} file: {path}", file=sys.stderr)

        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Failed to parse {label} file {path}: {e}", file=sys.stderr)
            sys.exit(1)
        except OSError as e:
            print(f"Failed to read {label} file {path}: {e}", file=sys.stderr)
            sys.exit(1)

    proxy_settings = _load_json_config(
        args.proxy_config, default_proxy_settings, "proxy config"
    )
    models = _load_json_config(
        args.models_config, default_models, "models config"
    )

    # Resolve classification config path
    if os.path.isabs(args.classification_config):
        classification_file = args.classification_config
    else:
        classification_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), args.classification_config
        )

    try:
        classification_config = classifier.load_config(classification_file)
    except ValueError as e:
        print(f"Invalid classification config: {e}", file=sys.stderr)
        sys.exit(1)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Failed to load classification config {classification_file}: {e}", file=sys.stderr)
        sys.exit(1)

    # Resolve journal config path
    if os.path.isabs(args.journal_config):
        journal_file = args.journal_config
    else:
        journal_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), args.journal_config
        )

    try:
        journal_cfg = journal_config.load_config(journal_file)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Failed to load journal config {journal_file}: {e}", file=sys.stderr)
        sys.exit(1)

    journal_store = None
    journal_bus = None
    gpu_poller = None
    gpu_stop = None
    system_poller = None
    system_stop = None
    auth_store = None
    ui_theme = "system"
    ui_show_gpu = False
    ui_show_system = False
    ui_state_colors = {}
    enable_gpu = bool(journal_cfg.get("enable_gpu", True))
    enable_system = bool(journal_cfg.get("enable_system", True))
    if journal_cfg.get("enabled"):
        journal_bus = EventBus()
        journal_store = JournalStore(
            max_bytes=journal_cfg["max_bytes"],
            max_age_seconds=journal_cfg["max_age_seconds"],
            bus=journal_bus,
        )
        journal_store.append(make_proxy_launched_entry(args.port), pinned=True)
        journal_store.start_eviction_tick()

        if enable_gpu:
            gpu_stop = threading.Event()

            def _gpu_on_sample(sample):
                journal_bus.publish("gpu_change", sample)

            gpu_poller = NvidiaSmiPoller(
                sampler=default_nvidia_smi_sampler,
                on_sample=_gpu_on_sample,
                interval=journal_cfg["gpu_poll_interval_seconds"],
                stop_event=gpu_stop,
            )
            gpu_poller.start()

        if enable_system:
            system_stop = threading.Event()

            def _system_on_sample(sample):
                journal_bus.publish("system_change", sample)

            system_poller = SystemInfoPoller(
                sampler=make_default_system_sampler(
                    disk_mounts=journal_cfg["disk_mounts"],
                ),
                on_sample=_system_on_sample,
                interval=journal_cfg["system_poll_interval_seconds"],
                stop_event=system_stop,
            )
            system_poller.start()

        ui_cfg = journal_cfg.get("ui", {})
        ui_theme = ui_cfg.get("theme", "system")
        ui_show_gpu = bool(ui_cfg.get("show_gpu", False))
        ui_show_system = bool(ui_cfg.get("show_system", False))
        ui_state_colors = ui_cfg.get("state_colors", {})
        if ui_cfg.get("enabled"):
            auth_store = AuthStore(
                ui_cfg["admin_password"],
                ui_cfg["session_ttl_seconds"],
            )

    # Build URL-to-color mapping
    color_enabled = args.color and "NO_COLOR" not in os.environ and sys.stdout.isatty()
    url_colors: dict[str, str] = build_url_colors(models) if color_enabled else {}

    # Set up logging
    if color_enabled:
        handler = ColorHandler(url_colors)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)-8s %(message)s",
            stream=sys.stdout,
        )

    server = ThreadingHTTPServer(
        ("0.0.0.0", args.port),
        lambda *args, **kwargs: ClaudeProxyHandler(
            *args,
            models=models,
            proxy_settings=proxy_settings,
            url_colors=url_colors,
            classification_config=classification_config,
            journal_config=journal_cfg,
            journal_store=journal_store,
            journal_bus=journal_bus,
            gpu_poller=gpu_poller,
            system_poller=system_poller,
            auth_store=auth_store,
            ui_theme=ui_theme,
            ui_show_gpu=ui_show_gpu,
            ui_show_system=ui_show_system,
            ui_enable_gpu=enable_gpu,
            ui_enable_system=enable_system,
            ui_state_colors=ui_state_colors,
            **kwargs,
        ),
    )

    # Graceful shutdown on SIGTERM (for containers)
    def _shutdown(s, signum=None):
        logger.info("Received signal %s, shutting down...", signum)
        if gpu_stop is not None:
            gpu_stop.set()
        if system_stop is not None:
            system_stop.set()
        if journal_store is not None:
            journal_store.stop_eviction_tick()
        server.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)

    logger.info("Starting Claude Code API Proxy on port %d", args.port)
    logger.info("Proxy config file: %s", args.proxy_config)
    logger.info("Models config file: %s", args.models_config)
    logger.info("Classification configuration file: %s", classification_file)
    if classification_config.get("enabled"):
        logger.info(
            "Classification ENABLED (default=%s, min_confidence=%.2f)",
            classification_config["default"], classification_config["min_confidence"],
        )
        method = classification_config.get("method", "internal")
        if method == "llm":
            logger.info(
                "Classification method=llm (llm_url=%s, llm_model=%s)",
                classification_config.get("llm_url"),
                classification_config.get("llm_model"),
            )
        else:
            logger.info("Classification method=%s", method)
    else:
        logger.info("Classification disabled")
    logger.info("Journal configuration file: %s", journal_file)
    if journal_store is not None:
        logger.info("Journal ENABLED (in-memory only; queryable at /__journal/*)")
    else:
        logger.info("Journal disabled")
    if auth_store is not None:
        logger.info("Journal UI ENABLED at /__ui/ (admin-password gated; /__journal/* now requires session)")
    else:
        logger.info("Journal UI disabled")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down the proxy server...")
    finally:
        if gpu_stop is not None:
            gpu_stop.set()
        if system_stop is not None:
            system_stop.set()
        if journal_store is not None:
            journal_store.stop_eviction_tick()
        server.server_close()


# ------------------------------------------------------------------
# JSON redaction helpers (module-level, called by ClaudeProxyHandler._redact_body)
# ------------------------------------------------------------------


def _redact_json(obj, fields, max_len=10240):
    """Recursively redact *fields* from a JSON-compatible object.

    - Dict values matching *fields* are replaced with ``"[redacted]"``.
    - For the ``"system"`` field inside ``messages`` arrays, only the
      ``content`` key of system-role messages is redacted.
    - Large string values are truncated to prevent journal bloat.

    *max_len* controls the maximum length for individual content strings.
    """

    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if isinstance(v, list) and k == "messages":
                result[k] = _redact_messages(v, fields, max_len)
            elif isinstance(v, str) and k in fields:
                result[k] = "[redacted]"
            else:
                result[k] = _redact_json(v, fields, max_len)
        return result

    if isinstance(obj, list):
        return [_redact_json(item, fields, max_len) for item in obj]

    if isinstance(obj, str) and len(obj) > max_len:
        return obj[: max_len - 6] + " [... truncated]"

    return obj


def _redact_messages(messages, fields, max_len):
    """Redact content in system-role messages inside a *messages* array."""
    out = []
    for msg in messages:
        if isinstance(msg, dict):
            role = msg.get("role", "")
            content = msg.get("content")
            if role == "system" and content is not None:
                if isinstance(content, str) and len(content) > max_len:
                    msg = dict(msg)
                    msg["content"] = content[: max_len - 6] + " [... truncated]"
                elif isinstance(content, str):
                    msg = dict(msg)
                    msg["content"] = "[redacted]"
            # Also redact any top-level sensitive fields on the message
            if role == "system":
                for k in fields:
                    if k not in ("system",) and k in msg:
                        msg[k] = "[redacted]"
        out.append(msg)
    return out


if __name__ == "__main__":
    main()
