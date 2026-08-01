"""Device enrollment, token verification and rate limiting.

Tokens are shown to the user exactly once at enrollment. Only a keyed hash
is stored, so a copy of the database does not yield usable credentials.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .database import Database
from .models import iso_utc, utc_now

__all__ = [
    "AuthError",
    "RateLimiter",
    "TokenManager",
    "redact_token",
]

TOKEN_PREFIX = "atl_"  # noqa: S105 - a format marker, not a credential
_TOKEN_BYTES = 32


class AuthError(Exception):
    """Authentication or authorisation failed."""

    def __init__(self, message: str, *, status: int = 401) -> None:
        super().__init__(message)
        self.status = status


def redact_token(token: str | None) -> str:
    """Render a token safe for logs. Never reveals any part of the value."""
    if not token:
        return "<absent>"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
    return f"<redacted len={len(token)} sha256:{digest}>"


class TokenManager:
    """Issues, verifies, rotates and revokes device tokens.

    Tokens are 256 bits of ``secrets`` randomness, so the stored value is a
    single HMAC-SHA256 rather than a slow KDF: there is nothing to brute
    force, and a per-request KDF would only add latency. The HMAC key (a
    "pepper") lives in the app's data directory, outside the database, so a
    leaked database alone cannot be used to test candidate tokens.
    """

    def __init__(self, database: Database, pepper_path: Path) -> None:
        self.db = database
        self._pepper = self._load_or_create_pepper(pepper_path)

    @staticmethod
    def _load_or_create_pepper(path: Path) -> bytes:
        if path.is_file():
            return path.read_bytes().strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        pepper = secrets.token_hex(32).encode("ascii")
        path.write_bytes(pepper)
        # Tightening the mode is best effort: some volume drivers do not
        # support it, and failing here would make the app unstartable.
        with contextlib.suppress(OSError):
            path.chmod(0o600)
        return pepper

    def _hash(self, token: str) -> str:
        return hmac.new(self._pepper, token.encode("utf-8"), hashlib.sha256).hexdigest()

    # -- enrollment ----------------------------------------------------

    def enroll(
        self, device_id: str, *, display_name: str = "", label: str = ""
    ) -> tuple[str, str]:
        """Create a device (if new) and issue a token.

        Returns ``(token_id, token)``. The plaintext token exists only in
        this return value -- it is never written anywhere.
        """
        self.db.upsert_device(device_id, display_name)
        token = TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_BYTES)
        token_id = f"tok_{uuid.uuid4()}"
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO device_tokens
                    (token_id, device_id, token_hash, algorithm, label, created_at_utc)
                VALUES (?, ?, ?, 'hmac-sha256', ?, ?)
                """,
                (token_id, device_id, self._hash(token), label, iso_utc(utc_now())),
            )
        return token_id, token

    def rotate(self, device_id: str, *, label: str = "rotated") -> tuple[str, str]:
        """Issue a new token and revoke every previous one for the device.

        Ordering matters: the new token is created first, so a rotation that
        crashes half way leaves the device able to authenticate rather than
        locked out.
        """
        token_id, token = self.enroll(device_id, label=label)
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE device_tokens SET revoked_at_utc = ? "
                "WHERE device_id = ? AND token_id != ? AND revoked_at_utc IS NULL",
                (iso_utc(utc_now()), device_id, token_id),
            )
        return token_id, token

    def revoke(self, token_id: str) -> bool:
        with self.db.transaction() as conn:
            before = conn.total_changes
            conn.execute(
                "UPDATE device_tokens SET revoked_at_utc = ? "
                "WHERE token_id = ? AND revoked_at_utc IS NULL",
                (iso_utc(utc_now()), token_id),
            )
            changed = conn.total_changes - before
        return changed > 0

    def revoke_all(self, device_id: str) -> int:
        with self.db.transaction() as conn:
            before = conn.total_changes
            conn.execute(
                "UPDATE device_tokens SET revoked_at_utc = ? "
                "WHERE device_id = ? AND revoked_at_utc IS NULL",
                (iso_utc(utc_now()), device_id),
            )
            changed = conn.total_changes - before
        return int(changed)

    def tokens(self, device_id: str) -> list[dict[str, Any]]:
        """Token metadata only. The hash is never returned."""
        rows = self.db.query(
            "SELECT token_id, device_id, algorithm, label, created_at_utc, "
            "last_used_utc, revoked_at_utc FROM device_tokens WHERE device_id = ? "
            "ORDER BY created_at_utc DESC",
            (device_id,),
        )
        return [dict(r) for r in rows]

    # -- verification --------------------------------------------------

    def verify(self, device_id: str, token: str) -> str:
        """Verify a bearer token for ``device_id``; returns the token id.

        Raises :class:`AuthError` with a message that never distinguishes
        "unknown device" from "wrong token" -- the caller learns only that
        authentication failed.
        """
        generic = "invalid device or token"

        if not token or not token.startswith(TOKEN_PREFIX):
            raise AuthError(generic)

        device = self.db.device(device_id)
        if device is None:
            raise AuthError(generic)
        if not device.get("enabled", 1):
            raise AuthError("device is disabled", status=403)

        candidate = self._hash(token)
        rows = self.db.query(
            "SELECT token_id, token_hash FROM device_tokens "
            "WHERE device_id = ? AND revoked_at_utc IS NULL",
            (device_id,),
        )
        for row in rows:
            if hmac.compare_digest(candidate, str(row["token_hash"])):
                token_id = str(row["token_id"])
                with self.db.transaction() as conn:
                    conn.execute(
                        "UPDATE device_tokens SET last_used_utc = ? WHERE token_id = ?",
                        (iso_utc(utc_now()), token_id),
                    )
                return token_id

        raise AuthError(generic)


@dataclass(slots=True)
class RateLimiter:
    """Fixed-window per-key request budget.

    Deliberately simple and in-process: this guards against a looping
    client, not against a distributed attacker. The app is not internet
    facing.
    """

    limit: int = 120
    window_seconds: int = 60
    _hits: dict[str, list[float]] = field(default_factory=dict)

    def check(self, key: str, *, now: float | None = None) -> None:
        moment = now if now is not None else time.monotonic()
        cutoff = moment - self.window_seconds
        hits = [t for t in self._hits.get(key, []) if t > cutoff]
        if len(hits) >= self.limit:
            hits.append(moment)
            self._hits[key] = hits
            raise AuthError(
                f"rate limit exceeded: more than {self.limit} requests in "
                f"{self.window_seconds}s",
                status=429,
            )
        hits.append(moment)
        self._hits[key] = hits

    def reset(self) -> None:
        self._hits.clear()
