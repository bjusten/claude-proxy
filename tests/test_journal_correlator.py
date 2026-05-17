"""Unit tests for journal.correlator.session_id_for.

Pure function: derives a stable session id from request headers + peer.

Header priority:
  anthropic-session-id > x-session-id > anthropic-trace-id > x-request-id
  → fallback: deterministic hash of (user-agent, peer)
"""

import unittest

from journal.correlator import session_id_for


class TestCorrelatorHeaderPriority(unittest.TestCase):
    def test_anthropic_session_id_wins_over_all_others(self):
        headers = {
            "anthropic-session-id": "sess-A",
            "x-session-id": "sess-X",
            "anthropic-trace-id": "trace-T",
            "x-request-id": "req-R",
        }
        self.assertEqual(session_id_for(headers, "1.2.3.4"), "sess-A")

    def test_x_session_id_wins_when_no_anthropic_session_id(self):
        headers = {
            "x-session-id": "sess-X",
            "anthropic-trace-id": "trace-T",
            "x-request-id": "req-R",
        }
        self.assertEqual(session_id_for(headers, "1.2.3.4"), "sess-X")

    def test_anthropic_trace_id_wins_when_no_session_headers(self):
        headers = {
            "anthropic-trace-id": "trace-T",
            "x-request-id": "req-R",
        }
        self.assertEqual(session_id_for(headers, "1.2.3.4"), "trace-T")

    def test_x_request_id_wins_when_no_other_trace_headers(self):
        headers = {"x-request-id": "req-R"}
        self.assertEqual(session_id_for(headers, "1.2.3.4"), "req-R")

    def test_header_match_is_case_insensitive(self):
        headers = {"Anthropic-Session-Id": "sess-A"}
        self.assertEqual(session_id_for(headers, "1.2.3.4"), "sess-A")


class TestCorrelatorFallback(unittest.TestCase):
    def test_same_user_agent_and_peer_produces_same_id(self):
        h = {"user-agent": "claude-code/1.0"}
        a = session_id_for(h, "1.2.3.4")
        b = session_id_for(h, "1.2.3.4")
        self.assertEqual(a, b)
        # Should NOT be None / empty
        self.assertTrue(a)

    def test_different_peer_produces_different_id(self):
        h = {"user-agent": "claude-code/1.0"}
        self.assertNotEqual(
            session_id_for(h, "1.2.3.4"),
            session_id_for(h, "5.6.7.8"),
        )

    def test_different_user_agent_produces_different_id(self):
        a = session_id_for({"user-agent": "claude-code/1.0"}, "1.2.3.4")
        b = session_id_for({"user-agent": "curl/8"}, "1.2.3.4")
        self.assertNotEqual(a, b)

    def test_no_headers_at_all_returns_stable_id(self):
        a = session_id_for({}, "")
        b = session_id_for({}, "")
        self.assertEqual(a, b)
        self.assertTrue(a)


if __name__ == "__main__":
    unittest.main()
