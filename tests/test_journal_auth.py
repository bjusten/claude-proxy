"""Unit tests for journal.auth.AuthStore."""

import unittest

from journal.auth import AuthStore


class _FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class _SeqTokens:
    def __init__(self):
        self.i = 0

    def __call__(self):
        self.i += 1
        return f"tok-{self.i}"


class TestAuthStoreLogin(unittest.TestCase):
    def test_correct_password_returns_non_empty_token(self):
        store = AuthStore("hunter2", ttl_seconds=60)
        token = store.login("hunter2")
        self.assertIsNotNone(token)
        self.assertTrue(token)

    def test_incorrect_password_returns_none(self):
        store = AuthStore("hunter2", ttl_seconds=60)
        self.assertIsNone(store.login("nope"))

    def test_empty_submitted_password_returns_none(self):
        store = AuthStore("hunter2", ttl_seconds=60)
        self.assertIsNone(store.login(""))
        self.assertIsNone(store.login(None))

    def test_empty_configured_password_rejects_all(self):
        store = AuthStore("", ttl_seconds=60)
        self.assertIsNone(store.login(""))
        self.assertIsNone(store.login("anything"))

    def test_none_configured_password_rejects_all(self):
        store = AuthStore(None, ttl_seconds=60)
        self.assertIsNone(store.login("anything"))

    def test_uses_constant_time_compare(self):
        # Verifies that the implementation references hmac.compare_digest.
        # If anyone replaces it with ==, the substring "compare_digest" will
        # disappear and this test will fail.
        import inspect

        from journal import auth

        src = inspect.getsource(auth)
        self.assertIn("compare_digest", src)


class TestAuthStoreValidate(unittest.TestCase):
    def test_unknown_token_invalid(self):
        store = AuthStore("p", ttl_seconds=60)
        self.assertFalse(store.validate("does-not-exist"))

    def test_empty_or_none_token_invalid(self):
        store = AuthStore("p", ttl_seconds=60)
        self.assertFalse(store.validate(""))
        self.assertFalse(store.validate(None))

    def test_fresh_token_valid(self):
        clock = _FakeClock()
        store = AuthStore("p", ttl_seconds=60, clock=clock, token_generator=_SeqTokens())
        token = store.login("p")
        self.assertTrue(store.validate(token))

    def test_token_expires_after_ttl(self):
        clock = _FakeClock()
        store = AuthStore("p", ttl_seconds=60, clock=clock, token_generator=_SeqTokens())
        token = store.login("p")
        clock.advance(59)
        self.assertTrue(store.validate(token))
        clock.advance(2)  # now past TTL
        self.assertFalse(store.validate(token))

    def test_expired_token_is_reaped(self):
        clock = _FakeClock()
        store = AuthStore("p", ttl_seconds=10, clock=clock, token_generator=_SeqTokens())
        t1 = store.login("p")
        clock.advance(11)
        store.validate(t1)  # triggers reap
        self.assertNotIn(t1, store._tokens)


class TestAuthStorePrehashedPassword(unittest.TestCase):
    def test_prehashed_password_config_works(self):
        """Config value already prefixed with $hash: should work directly."""
        import hashlib

        digest = hashlib.sha256("secret".encode()).hexdigest()
        prehashed = f"$hash:{digest}"
        store = AuthStore(prehashed, ttl_seconds=60)
        self.assertIsNotNone(store.login("secret"))
        self.assertIsNone(store.login("wrong"))

    def test_sha256_hash_matches_when_bcrypt_available(self):
        """Sha256-stored hash must verify correctly even when bcrypt is installed.

        Regression: _hash_password prefers bcrypt, so hashing 'blah' via
        _hash_password() would produce a bcrypt hash that never matches a
        sha256-stored digest. _check_password must compute sha256 directly.
        """
        import hashlib

        digest = hashlib.sha256("blah".encode()).hexdigest()
        prehashed = f"$hash:{digest}"
        store = AuthStore(prehashed, ttl_seconds=60)
        self.assertIsNotNone(store.login("blah"))
        self.assertIsNone(store.login("blahx"))
        self.assertIsNone(store.login("BLAH"))

    def test_plain_sha256_hash_without_prefix(self):
        """Plain sha256 hex digest (no $hash: prefix) should also work."""
        import hashlib

        digest = hashlib.sha256("secret".encode()).hexdigest()
        store = AuthStore(digest, ttl_seconds=60)
        self.assertIsNotNone(store.login("secret"))
        self.assertIsNone(store.login("wrong"))

    def test_plaintext_config_stored_as_is(self):
        """Plaintext password config should be stored as-is and matched directly."""
        store = AuthStore("secret", ttl_seconds=60)
        self.assertTrue(store.login("secret"))
        self.assertEqual(store._password_hash, "secret")
        self.assertFalse(store.login("wrong"))


class TestAuthStoreLogout(unittest.TestCase):
    def test_logout_invalidates_token(self):
        store = AuthStore("p", ttl_seconds=60)
        token = store.login("p")
        self.assertTrue(store.validate(token))
        store.logout(token)
        self.assertFalse(store.validate(token))

    def test_logout_unknown_token_is_noop(self):
        store = AuthStore("p", ttl_seconds=60)
        store.logout("never-issued")  # must not raise

    def test_logout_empty_token_is_noop(self):
        store = AuthStore("p", ttl_seconds=60)
        store.logout("")
        store.logout(None)


if __name__ == "__main__":
    unittest.main()
