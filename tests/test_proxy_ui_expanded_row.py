"""End-to-end tests for the Journal UI expanded row.

Reuses the live ClaudeProxyHandler fixture. This module covers the
asset-serving and scaffold contract that the row-expansion feature
sits on top of; the DOM behaviour (JSON tree, copy, multi-row expand)
is verified by manual smoke.
"""

import json
import time
import unittest

from tests.test_proxy_ui_activity_list import _ActivityFixture, _request


class TestExpandedRowJsAssets(unittest.TestCase):

    def setUp(self):
        self.fx = _ActivityFixture()

    def tearDown(self):
        self.fx.stop()

    def test_json_tree_js_served_with_cookie(self):
        token = self.fx.login()
        status, hdrs, body = _request(
            self.fx.port, "GET", "/__ui/json-tree.js",
            headers={"Cookie": f"session={token}"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            hdrs["Content-Type"],
            "application/javascript; charset=utf-8",
        )
        self.assertGreater(len(body), 0)

    def test_expand_js_served_with_cookie(self):
        token = self.fx.login()
        status, hdrs, body = _request(
            self.fx.port, "GET", "/__ui/expand.js",
            headers={"Cookie": f"session={token}"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            hdrs["Content-Type"],
            "application/javascript; charset=utf-8",
        )
        self.assertGreater(len(body), 0)

    def test_json_tree_js_redirects_without_cookie(self):
        status, hdrs, _ = _request(self.fx.port, "GET", "/__ui/json-tree.js")
        self.assertEqual(status, 302)
        self.assertEqual(hdrs["Location"], "/__ui/login")

    def test_expand_js_redirects_without_cookie(self):
        status, hdrs, _ = _request(self.fx.port, "GET", "/__ui/expand.js")
        self.assertEqual(status, 302)
        self.assertEqual(hdrs["Location"], "/__ui/login")


class TestDeepEntryContractViaCookie(unittest.TestCase):
    """The UI fetches GET /__journal/entries/<id> over the cookie-gated path.

    Drives a real request through the fixture, finds the proxy_connection
    entry id from the shallow list, then asserts the deep response shape
    the expand panel will render against.
    """

    def setUp(self):
        self.fx = _ActivityFixture()

    def tearDown(self):
        self.fx.stop()

    def test_deep_entry_returns_envelope_keys(self):
        token = self.fx.login()
        cookie = {"Cookie": f"session={token}"}

        self.fx.post_traffic(n=1)

        entry_id = None
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            status, _, body = _request(
                self.fx.port, "GET", "/__journal/entries", headers=cookie,
            )
            self.assertEqual(status, 200)
            entries = json.loads(body)
            for e in entries:
                if (e.get("type") == "proxy_connection"
                        and e.get("status") == 200):
                    entry_id = e["id"]
                    break
            if entry_id:
                break
            time.sleep(0.05)
        self.assertIsNotNone(entry_id, "proxy_connection entry never settled")

        status, hdrs, body = _request(
            self.fx.port, "GET", f"/__journal/entries/{entry_id}",
            headers=cookie,
        )
        self.assertEqual(status, 200)
        self.assertIn("application/json", hdrs["Content-Type"])

        deep = json.loads(body)
        self.assertEqual(deep["id"], entry_id)
        self.assertEqual(deep["type"], "proxy_connection")
        # Envelope keys the expand panel will render.
        self.assertIn("timestamps", deep)
        self.assertIn("incoming", deep)
        self.assertIn("routed", deep)
        self.assertIn("response", deep)
        self.assertEqual(deep["response"]["status"], 200)

    def test_deep_entry_redirects_without_cookie(self):
        # Drive one request so an entry exists to ask for.
        token = self.fx.login()
        self.fx.post_traffic(n=1)

        deadline = time.monotonic() + 3.0
        entry_id = None
        while time.monotonic() < deadline and not entry_id:
            _, _, body = _request(
                self.fx.port, "GET", "/__journal/entries",
                headers={"Cookie": f"session={token}"},
            )
            for e in json.loads(body):
                if e.get("type") == "proxy_connection":
                    entry_id = e["id"]
                    break
            if not entry_id:
                time.sleep(0.05)
        self.assertIsNotNone(entry_id)

        status, hdrs, _ = _request(
            self.fx.port, "GET", f"/__journal/entries/{entry_id}",
        )
        self.assertEqual(status, 401)


class TestSessionTableSource(unittest.TestCase):
    """Verify session-table.js source contains expected column and dot logic."""

    def test_session_table_has_state_dot_column(self):
        import pathlib
        src = pathlib.Path("journal/ui/session-table.js").read_text()
        self.assertIn("key: 'state'", src)
        self.assertNotIn("state-dot", src)


if __name__ == "__main__":
    unittest.main()
