"""Admin-password session auth for the journal UI.

Single-tenant: one configured password, many active session tokens.
Tokens are opaque and stored in-memory with an absolute expiry.

Password storage uses bcrypt (preferred) or hashlib.sha256 (fallback) to
derive a one-way hash.  The hash is stored in journal.json, and login
compares the submitted-password hash against the stored hash.  This avoids
storing plaintext credentials on disk.

Rate limiting: failed login attempts are tracked per source IP with a
sliding window (10 attempts per 60s). Once exceeded, further attempts
are rejected until the window slides below the threshold.

CSRF tokens: opaque random strings generated per login page GET and
validated on the corresponding POST.
"""

import hmac
import secrets
import threading
import time

try:
    import bcrypt  # noqa: F401
    _USE_BCRYPT = True
except ImportError:
    import hashlib  # noqa: F401
    _USE_BCRYPT = False


def _hash_password(plaintext):
    """Hash *plaintext* using bcrypt (preferred) or sha256 (fallback).

    Returns a string prefixed with ``$hash:`` to distinguish pre-hashed
    values from plaintext on subsequent reads.
    """
    salt = bcrypt.gensalt() if _USE_BCRYPT else None
    raw = bcrypt.hashpw(plaintext.encode(), salt).decode() if _USE_BCRYPT else None
    if raw:
        return f"$hash:{raw}"
    h = hashlib.sha256(plaintext.encode()).hexdigest()
    return f"$hash:{h}"


def _check_password(password_hash, plaintext):
    """Return True if *plaintext* matches the stored *password_hash*.

    Detects the ``$hash:`` prefix: if present the value is treated as an
    already-derived hash and compared directly; otherwise the plaintext is
    hashed first and then compared.

    Uses ``bcrypt.checkpw`` when available, falling back to
    ``hmac.compare_digest`` on the sha256 digests.
    """
    if not password_hash or not plaintext:
        return False

    if password_hash.startswith("$hash:"):
        _, stored = password_hash.split(":", 1)
        # Treat the stored value as a bcrypt hash if it starts with $2b or $2a.
        if stored.startswith("$2b$") or stored.startswith("$2a$"):
            if _USE_BCRYPT:
                return bcrypt.checkpw(plaintext.encode(), stored.encode())
            else:
                # Fallback: hash the input the same way and compare.
                plain_hash = _hash_password(plaintext).split(":", 1)[1]
                return hmac.compare_digest(plain_hash.encode(), stored.encode())
        # sha256 fallback
        plain_hash = _hash_password(plaintext).split(":", 1)[1]
        return hmac.compare_digest(plain_hash.encode(), stored.encode())

    # Plaintext password — hash it now.
    return _check_password(_hash_password(plaintext), plaintext)


class AuthStore:
    def __init__(self, password, ttl_seconds, clock=None, token_generator=None):
        # Store a hashed password (prefixed with "$hash:" for detection).
        # If the incoming password is already a hash, it passes through unchanged.
        if password and not password.startswith("$hash:"):
            self._password_hash = _hash_password(password)
        else:
            self._password_hash = password or ""
        self._ttl = ttl_seconds
        self._clock = clock or time.time
        self._token_generator = token_generator or secrets.token_urlsafe
        self._tokens: dict[str, float] = {}
        self._lock = threading.Lock()

        # Rate limiting: max login attempts per IP within a sliding window.
        self._max_attempts = 10
        self._window_seconds = 60  # 10 attempts per 60s window
        self._attempts: dict[str, list[float]] = {}  # ip -> [timestamps]

    @property
    def ttl(self):
        """TTL in seconds, exposed for ui_server."""
        return self._ttl

    def generate_csrf_token(self) -> str:
        """Generate a random CSRF token."""
        return secrets.token_urlsafe(32)

    def validate_csrf(self, token: str, secret: str) -> bool:
        """Validate a CSRF token using constant-time comparison."""
        if not token or not secret:
            return False
        return hmac.compare_digest(token.encode("utf-8"), secret.encode("utf-8"))

    def check_rate_limit(self, client_ip: str = "") -> bool:
        """Return True if the request is allowed (not rate-limited).

        Tracks failed login attempts per source IP. Resets after the window
        expires and drops the oldest entries below the threshold.
        """
        if not client_ip:
            return True  # no IP to track, allow
        now = self._clock()
        with self._lock:
            window_start = now - self._window_seconds
            attempts = self._attempts.get(client_ip, [])
            # Prune entries outside the sliding window.
            self._attempts[client_ip] = [t for t in attempts if t > window_start]
            attempts = self._attempts[client_ip]
            if len(attempts) >= self._max_attempts:
                return False
            return True

    def record_failed_attempt(self, client_ip: str = ""):
        """Record a failed login attempt for rate limiting."""
        if not client_ip:
            return
        now = self._clock()
        with self._lock:
            attempts = self._attempts.get(client_ip, [])
            window_start = now - self._window_seconds
            self._attempts[client_ip] = [t for t in attempts if t > window_start]
            self._attempts[client_ip].append(now)

    def clear_failed_attempts(self, client_ip: str = ""):
        """Clear failed attempts after a successful login."""
        if not client_ip:
            return
        with self._lock:
            self._attempts.pop(client_ip, None)

    def login(self, submitted_password):
        if not self._password_hash:
            return None
        if not _check_password(self._password_hash, submitted_password):
            return None
        token = self._token_generator()
        with self._lock:
            self._tokens[token] = self._clock() + self._ttl
        return token

    def validate(self, token):
        if not token:
            return False
        now = self._clock()
        with self._lock:
            self._reap(now)
            expires = self._tokens.get(token)
            if expires is None:
                return False
            if expires <= now:
                self._tokens.pop(token, None)
                return False
            return True

    def logout(self, token):
        if not token:
            return
        with self._lock:
            self._tokens.pop(token, None)

    def _reap(self, now):
        expired = [t for t, exp in self._tokens.items() if exp <= now]
        for t in expired:
            self._tokens.pop(t, None)
