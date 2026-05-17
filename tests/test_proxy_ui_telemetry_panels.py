"""End-to-end tests for the Journal UI GPU/system telemetry panels.

Pins the contract-level surfaces the GPU/system telemetry, status bar, and
session grouping features sit on top of:

  - app.js subscribes to all three SSE topics
  - sse.js registers per-topic listeners for gpu_change and system_change
  - index.html exposes the toggle, panel, and status-bar scaffolds
  - the telemetry.js/status-bar.js/session-table.js modules are cookie-gated
    and redirect to /__ui/login otherwise
  - app.js imports each of those modules

DOM behaviour (toggle persistence, panel field rendering, uptime tick,
session-grouped activity table) is verified by manual smoke.
"""

import unittest

from tests.test_proxy_ui_activity_list import _ActivityFixture, _request


class TestSseSubscriptionUrl(unittest.TestCase):
    """app.js subscribes to journal_activity + gpu_change + system_change."""

    def setUp(self):
        self.fx = _ActivityFixture()

    def tearDown(self):
        self.fx.stop()

    def test_app_js_subscribes_to_all_three_topics(self):
        token = self.fx.login()
        status, _, body = _request(
            self.fx.port, "GET", "/__ui/app.js",
            headers={"Cookie": f"session={token}"},
        )
        self.assertEqual(status, 200)
        self.assertIn(b"topic=journal_activity", body)
        self.assertIn(b"topic=gpu_change", body)
        self.assertIn(b"topic=system_change", body)


class TestIndexHasTelemetryToggles(unittest.TestCase):
    """index.html ships `show-gpu` and `show-system` toggle checkboxes.

    Each is a real form input with the documented id so JS code can bind
    listeners and localStorage state can round-trip without scraping
    arbitrary chrome.
    """

    def setUp(self):
        self.fx = _ActivityFixture()

    def tearDown(self):
        self.fx.stop()

    def test_index_has_show_gpu_checkbox(self):
        token = self.fx.login()
        status, _, body = _request(
            self.fx.port, "GET", "/__ui/",
            headers={"Cookie": f"session={token}"},
        )
        self.assertEqual(status, 200)
        self.assertIn(b'id="show-gpu"', body)
        self.assertIn(b'type="checkbox"', body)

    def test_index_has_show_system_checkbox(self):
        token = self.fx.login()
        status, _, body = _request(
            self.fx.port, "GET", "/__ui/",
            headers={"Cookie": f"session={token}"},
        )
        self.assertEqual(status, 200)
        self.assertIn(b'id="show-system"', body)


class TestIndexHasTelemetryPanelsAndStatusBar(unittest.TestCase):
    """index.html ships hidden GPU + system panels and a status bar.

    The panels default to hidden; toggling reveals them. The status bar
    is always visible and hosts the pinned `proxy_launched` summary.
    """

    def setUp(self):
        self.fx = _ActivityFixture()

    def tearDown(self):
        self.fx.stop()

    def _index(self):
        token = self.fx.login()
        status, _, body = _request(
            self.fx.port, "GET", "/__ui/",
            headers={"Cookie": f"session={token}"},
        )
        self.assertEqual(status, 200)
        return body

    def test_index_has_status_bar(self):
        body = self._index()
        self.assertIn(b'id="status-bar"', body)

    def test_index_has_hidden_gpu_panel(self):
        body = self._index()
        self.assertIn(b'id="gpu-panel"', body)
        # Panel ships hidden so first paint matches the default-off toggle.
        self.assertRegex(
            body, rb'id="gpu-panel"[^>]*\bhidden\b',
        )

    def test_index_has_hidden_system_panel(self):
        body = self._index()
        self.assertIn(b'id="system-panel"', body)
        self.assertRegex(
            body, rb'id="system-panel"[^>]*\bhidden\b',
        )


class TestTelemetryPanelHiddenWins(unittest.TestCase):
    """style.css must let the `hidden` attribute beat `.telemetry-panel`.

    Regression for the toggles going dead: `.telemetry-panel` sets
    `display: flex`, which outranks the UA `[hidden] { display: none }`
    rule, so `showPanel()` toggling `el.hidden` had no visual effect and
    the panels were always on screen. A scoped `.telemetry-panel[hidden]`
    rule restores the toggle.
    """

    def setUp(self):
        self.fx = _ActivityFixture()

    def tearDown(self):
        self.fx.stop()

    def test_style_css_scopes_hidden_override(self):
        token = self.fx.login()
        status, _, body = _request(
            self.fx.port, "GET", "/__ui/style.css",
            headers={"Cookie": f"session={token}"},
        )
        self.assertEqual(status, 200)
        self.assertRegex(
            body,
            rb'\.telemetry-panel\[hidden\]\s*\{[^}]*display:\s*none',
        )


class TestNewModulesServed(unittest.TestCase):
    """telemetry.js / status-bar.js / session-table.js are cookie-gated."""

    UI_MODULES = ("telemetry.js", "status-bar.js", "session-table.js")

    def setUp(self):
        self.fx = _ActivityFixture()

    def tearDown(self):
        self.fx.stop()

    def test_modules_served_with_cookie(self):
        token = self.fx.login()
        for name in self.UI_MODULES:
            with self.subTest(module=name):
                status, hdrs, body = _request(
                    self.fx.port, "GET", f"/__ui/{name}",
                    headers={"Cookie": f"session={token}"},
                )
                self.assertEqual(status, 200)
                self.assertEqual(
                    hdrs["Content-Type"],
                    "application/javascript; charset=utf-8",
                )
                self.assertGreater(len(body), 0)

    def test_modules_redirect_without_cookie(self):
        for name in self.UI_MODULES:
            with self.subTest(module=name):
                status, hdrs, _ = _request(
                    self.fx.port, "GET", f"/__ui/{name}",
                )
                self.assertEqual(status, 302)
                self.assertEqual(hdrs["Location"], "/__ui/login")


class TestAppJsImportsUiModules(unittest.TestCase):
    """app.js wires the telemetry/status-bar/session-table imports so chrome
    features can hook in.

    Cheap regression — keeps the import edges from getting dropped during
    refactor, the same way `./expand.js` is pinned by the expanded-row suite.
    """

    def setUp(self):
        self.fx = _ActivityFixture()

    def tearDown(self):
        self.fx.stop()

    def test_app_js_imports_ui_modules(self):
        token = self.fx.login()
        status, _, body = _request(
            self.fx.port, "GET", "/__ui/app.js",
            headers={"Cookie": f"session={token}"},
        )
        self.assertEqual(status, 200)
        self.assertIn(b"./telemetry.js", body)
        self.assertIn(b"./status-bar.js", body)
        self.assertIn(b"./session-table.js", body)


class TestSseModuleFansOutNewTopics(unittest.TestCase):
    """sse.js registers per-topic listeners for gpu_change + system_change.

    Mirrors the journal_activity listener pattern already in place. Each
    topic name appears as a string literal so addEventListener wiring
    can't silently regress to message-only.
    """

    def setUp(self):
        self.fx = _ActivityFixture()

    def tearDown(self):
        self.fx.stop()

    def test_sse_js_registers_listeners_for_all_topics(self):
        token = self.fx.login()
        status, _, body = _request(
            self.fx.port, "GET", "/__ui/sse.js",
            headers={"Cookie": f"session={token}"},
        )
        self.assertEqual(status, 200)
        self.assertIn(b"'journal_activity'", body)
        self.assertIn(b"'gpu_change'", body)
        self.assertIn(b"'system_change'", body)


if __name__ == "__main__":
    unittest.main()
