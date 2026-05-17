"""Unit tests for journal.ui_server.dispatch_ui and cookie helpers."""

import unittest

from journal.auth import AuthStore
from journal.ui_server import (
    dispatch_ui,
    extract_session_cookie,
    make_session_cookie,
    make_logout_cookie,
)


def _stub_assets():
    """Return an asset_loader that serves known fixed content."""
    fixtures = {
        "login.html": b"<html>LOGIN<!--ERROR--></html>",
        "index.html": b"<html>INDEX<script><!--THEME--></script></html>",
        "style.css": b"body{}",
        "app.js": b"// app",
    }

    def load(name):
        return fixtures.get(name)
    return load


def _login(store, password="hunter2"):
    return store.login(password)


def _cookie(token):
    return {"Cookie": f"session={token}"}


def _dispatch(command, path, *, store=None, headers=None, body=b"", loader=None,
              ui_theme="system", classification_enabled=False,
              ui_enable_gpu=True, ui_enable_system=True,
              ui_state_colors=None):
    if store is None:
        store = AuthStore("hunter2", ttl_seconds=60)
    return dispatch_ui(
        command, path, headers or {}, body, store,
        asset_loader=loader or _stub_assets(), ui_theme=ui_theme,
        classification_enabled=classification_enabled,
        ui_enable_gpu=ui_enable_gpu, ui_enable_system=ui_enable_system,
        ui_state_colors=ui_state_colors,
    )


class TestExtractCookie(unittest.TestCase):
    def test_no_cookie_header_returns_none(self):
        self.assertIsNone(extract_session_cookie({}))

    def test_no_session_cookie_returns_none(self):
        self.assertIsNone(extract_session_cookie({"Cookie": "foo=bar"}))

    def test_session_cookie_returned(self):
        self.assertEqual(extract_session_cookie({"Cookie": "session=abc"}), "abc")

    def test_session_cookie_among_others(self):
        self.assertEqual(
            extract_session_cookie({"Cookie": "x=1; session=abc; y=2"}),
            "abc",
        )

    def test_host_session_cookie_returned(self):
        self.assertEqual(
            extract_session_cookie({"Cookie": "session=xyz"}),
            "xyz",
        )

    def test_case_insensitive_header_name(self):
        self.assertEqual(extract_session_cookie({"cookie": "session=zz"}), "zz")

    def test_blank_session_returns_none(self):
        self.assertIsNone(extract_session_cookie({"Cookie": "session="}))


class TestMakeCookies(unittest.TestCase):
    def test_session_cookie_has_security_attrs(self):
        c = make_session_cookie("tok", 60)
        self.assertIn("session=tok", c)
        self.assertIn("HttpOnly", c)
        self.assertIn("Path=/", c)
        self.assertIn("SameSite=Strict", c)
        self.assertIn("Max-Age=60", c)

    def test_logout_cookie_expires_immediately(self):
        c = make_logout_cookie()
        self.assertIn("session=", c)
        self.assertIn("Max-Age=0", c)
        self.assertIn("HttpOnly", c)
        self.assertIn("Path=/", c)
        self.assertIn("SameSite=Strict", c)


class TestDispatchUiLogin(unittest.TestCase):
    def test_get_login_returns_200_login_html(self):
        status, headers, body = _dispatch("GET", "/__ui/login")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"LOGIN", body)
        # No error rendered when arriving fresh.
        self.assertNotIn(b"Incorrect password", body)

    def test_get_login_reachable_without_cookie(self):
        # Even with a fresh AuthStore that has no tokens, GET /__ui/login works.
        status, _, _ = _dispatch("GET", "/__ui/login")
        self.assertEqual(status, 200)

    def test_post_login_correct_password_redirects_and_sets_cookie(self):
        store = AuthStore("hunter2", ttl_seconds=60)
        # Simulate GET → CSRF cookie, then POST with it.
        csrf_token = store.generate_csrf_token()
        status, headers, body = _dispatch(
            "POST", "/__ui/login", store=store,
            headers={"Cookie": f"csrf={csrf_token}"},
            body=f"password=hunter2&csrf_token={csrf_token}".encode(),
        )
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/__ui/")
        cookie = headers["Set-Cookie"]
        self.assertIn("session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Path=/", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Max-Age=60", cookie)

    def test_post_login_wrong_password_renders_login_no_cookie(self):
        store = AuthStore("hunter2", ttl_seconds=60)
        status, headers, body = _dispatch(
            "POST", "/__ui/login", store=store, body=b"password=nope",
        )
        self.assertEqual(status, 200)
        self.assertNotIn("Set-Cookie", headers)
        self.assertIn(b"Incorrect password", body)

    def test_post_login_blank_field_renders_same_generic_error(self):
        store = AuthStore("hunter2", ttl_seconds=60)
        _, _, body_blank = _dispatch(
            "POST", "/__ui/login", store=store, body=b"password=",
        )
        _, _, body_wrong = _dispatch(
            "POST", "/__ui/login", store=store, body=b"password=nope",
        )
        # Generic message — no distinction between blank and wrong.
        self.assertIn(b"Incorrect password", body_blank)
        self.assertIn(b"Incorrect password", body_wrong)
        self.assertEqual(body_blank, body_wrong)


class TestDispatchUiIndex(unittest.TestCase):
    def test_get_index_no_cookie_redirects_to_login(self):
        status, headers, _ = _dispatch("GET", "/__ui/")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/__ui/login")

    def test_get_index_with_invalid_cookie_redirects_to_login(self):
        status, headers, _ = _dispatch(
            "GET", "/__ui/", headers=_cookie("bogus"),
        )
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/__ui/login")

    def test_get_index_with_valid_cookie_returns_index_html(self):
        store = AuthStore("hunter2", ttl_seconds=60)
        token = _login(store)
        status, headers, body = _dispatch(
            "GET", "/__ui/", store=store, headers=_cookie(token),
        )
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"INDEX", body)


class TestDispatchUiThemeInjection(unittest.TestCase):
    def _index_body(self, ui_theme):
        store = AuthStore("hunter2", ttl_seconds=60)
        token = _login(store)
        _, _, body = _dispatch(
            "GET", "/__ui/", store=store, headers=_cookie(token),
            ui_theme=ui_theme,
        )
        return body

    def test_default_theme_injected(self):
        self.assertIn(b'window.__themeDefault = "system";', self._index_body("system"))

    def test_explicit_theme_injected(self):
        self.assertIn(b'window.__themeDefault = "dark";', self._index_body("dark"))
        self.assertNotIn(b"<!--THEME-->", self._index_body("dark"))

    def test_invalid_theme_falls_back_to_system(self):
        self.assertIn(
            b'window.__themeDefault = "system";',
            self._index_body("'; alert(1) //"),
        )


class TestDispatchUiClassificationInjection(unittest.TestCase):
    """index.html carries the operator's classification flag so the client
    can hide the classified column / routing field when it's off."""

    def _index_body(self, classification_enabled):
        store = AuthStore("hunter2", ttl_seconds=60)
        token = _login(store)
        _, _, body = _dispatch(
            "GET", "/__ui/", store=store, headers=_cookie(token),
            classification_enabled=classification_enabled,
        )
        return body

    def test_disabled_by_default(self):
        body = self._index_body(False)
        self.assertIn(b"window.__classificationEnabled = false;", body)
        self.assertNotIn(b"<!--THEME-->", body)

    def test_enabled_when_configured(self):
        self.assertIn(
            b"window.__classificationEnabled = true;",
            self._index_body(True),
        )

    def test_theme_still_injected_alongside(self):
        # The classification flag rides the same injected <script> as theme.
        self.assertIn(b'window.__themeDefault = "system";', self._index_body(False))


class TestDispatchUiTelemetryEnabledInjection(unittest.TestCase):
    """index.html carries the operator's enable_gpu / enable_system flags so
    the client hides the GPU / system toggle when its poller is off."""

    def _index_body(self, *, ui_enable_gpu=True, ui_enable_system=True):
        store = AuthStore("hunter2", ttl_seconds=60)
        token = _login(store)
        _, _, body = _dispatch(
            "GET", "/__ui/", store=store, headers=_cookie(token),
            ui_enable_gpu=ui_enable_gpu, ui_enable_system=ui_enable_system,
        )
        return body

    def test_enabled_by_default(self):
        body = self._index_body()
        self.assertIn(b"window.__gpuEnabled = true;", body)
        self.assertIn(b"window.__systemEnabled = true;", body)
        self.assertNotIn(b"<!--THEME-->", body)

    def test_gpu_disabled_when_configured(self):
        body = self._index_body(ui_enable_gpu=False)
        self.assertIn(b"window.__gpuEnabled = false;", body)
        self.assertIn(b"window.__systemEnabled = true;", body)

    def test_system_disabled_when_configured(self):
        body = self._index_body(ui_enable_system=False)
        self.assertIn(b"window.__gpuEnabled = true;", body)
        self.assertIn(b"window.__systemEnabled = false;", body)

    def test_theme_still_injected_alongside(self):
        self.assertIn(b'window.__themeDefault = "system";', self._index_body())


class TestDispatchUiStateColorsInjection(unittest.TestCase):
    """index.html carries __stateColors so the client can colour-code rows."""

    def _index_body_with_state_colors(self, colors):
        store = AuthStore("hunter2", ttl_seconds=60)
        token = _login(store)
        _, _, body = _dispatch(
            "GET", "/__ui/", store=store, headers=_cookie(token),
            ui_state_colors=colors,
        )
        return body

    def test_state_colors_injected(self):
        body = self._index_body_with_state_colors({"INIT": "#9e9e9e", "SUCCESS": "#4caf50"})
        self.assertIn(b'window.__stateColors = {', body)
        self.assertIn(b'"INIT": "#9e9e9e"', body)

    def test_invalid_state_color_falls_back_to_default(self):
        body = self._index_body_with_state_colors({"INIT": "javascript:alert(1)", "SUCCESS": "#4caf50"})
        self.assertNotIn(b"javascript", body)
        self.assertIn(b'"INIT": "#9e9e9e"', body)


class TestDispatchUiAssets(unittest.TestCase):
    def test_authed_asset_returns_bytes_with_correct_content_type(self):
        store = AuthStore("hunter2", ttl_seconds=60)
        token = _login(store)
        status, headers, body = _dispatch(
            "GET", "/__ui/style.css", store=store, headers=_cookie(token),
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/css; charset=utf-8")
        self.assertEqual(body, b"body{}")

    def test_unauthed_asset_redirects_to_login(self):
        status, headers, _ = _dispatch("GET", "/__ui/app.js")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/__ui/login")

    def test_public_asset_served_without_auth(self):
        status, headers, body = _dispatch("GET", "/__ui/style.css")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/css; charset=utf-8")
        self.assertEqual(body, b"body{}")

    def test_unknown_asset_returns_404(self):
        store = AuthStore("hunter2", ttl_seconds=60)
        token = _login(store)
        status, _, _ = _dispatch(
            "GET", "/__ui/no-such-thing.css",
            store=store, headers=_cookie(token),
        )
        self.assertEqual(status, 404)

    def test_path_traversal_asset_rejected(self):
        store = AuthStore("hunter2", ttl_seconds=60)
        token = _login(store)
        status, _, _ = _dispatch(
            "GET", "/__ui/../config.py",
            store=store, headers=_cookie(token),
        )
        self.assertEqual(status, 404)


class TestDispatchUiLogout(unittest.TestCase):
    def test_post_logout_with_valid_cookie_redirects_and_clears_token(self):
        store = AuthStore("hunter2", ttl_seconds=60)
        token = _login(store)
        self.assertTrue(store.validate(token))
        status, headers, _ = _dispatch(
            "POST", "/__ui/logout", store=store, headers=_cookie(token),
        )
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/__ui/login")
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertFalse(store.validate(token))

    def test_post_logout_without_cookie_still_redirects_to_login(self):
        status, headers, _ = _dispatch("POST", "/__ui/logout")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/__ui/login")
        self.assertIn("Max-Age=0", headers["Set-Cookie"])


if __name__ == "__main__":
    unittest.main()
