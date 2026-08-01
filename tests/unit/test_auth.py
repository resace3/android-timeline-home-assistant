"""Token issuance, verification, rotation, revocation and rate limiting."""

from __future__ import annotations

import pytest

from android_timeline.app.auth import (
    TOKEN_PREFIX,
    AuthError,
    RateLimiter,
    TokenManager,
    redact_token,
)
from android_timeline.app.database import Database

DEVICE = "device-test-001"


class TestEnrollment:
    def test_issues_a_prefixed_high_entropy_token(self, tokens: TokenManager) -> None:
        _, token = tokens.enroll(DEVICE)
        assert token.startswith(TOKEN_PREFIX)
        assert len(token) > 40

    def test_two_enrollments_never_collide(self, tokens: TokenManager) -> None:
        _, first = tokens.enroll(DEVICE)
        _, second = tokens.enroll(DEVICE)
        assert first != second

    def test_plaintext_token_is_never_stored(
        self, tokens: TokenManager, database: Database
    ) -> None:
        _, token = tokens.enroll(DEVICE)
        rows = database.query("SELECT * FROM device_tokens")
        serialised = " ".join(str(dict(r)) for r in rows)
        assert token not in serialised
        assert rows[0]["algorithm"] == "hmac-sha256"

    def test_token_metadata_never_includes_the_hash(self, tokens: TokenManager) -> None:
        tokens.enroll(DEVICE)
        for record in tokens.tokens(DEVICE):
            assert "token_hash" not in record


class TestVerification:
    def test_correct_token_verifies(self, tokens: TokenManager) -> None:
        token_id, token = tokens.enroll(DEVICE)
        assert tokens.verify(DEVICE, token) == token_id

    def test_wrong_token_is_refused(self, tokens: TokenManager) -> None:
        tokens.enroll(DEVICE)
        with pytest.raises(AuthError):
            tokens.verify(DEVICE, TOKEN_PREFIX + "wrong")

    def test_unknown_device_is_refused(self, tokens: TokenManager) -> None:
        _, token = tokens.enroll(DEVICE)
        with pytest.raises(AuthError):
            tokens.verify("device-test-999", token)

    def test_failure_messages_do_not_distinguish_the_cause(self, tokens: TokenManager) -> None:
        # An attacker must not learn whether a device id exists.
        _, token = tokens.enroll(DEVICE)
        with pytest.raises(AuthError) as unknown_device:
            tokens.verify("device-test-999", token)
        with pytest.raises(AuthError) as wrong_token:
            tokens.verify(DEVICE, TOKEN_PREFIX + "nope")
        assert str(unknown_device.value) == str(wrong_token.value)

    def test_a_token_is_scoped_to_its_device(self, tokens: TokenManager) -> None:
        _, token_a = tokens.enroll("device-test-001")
        tokens.enroll("device-test-002")
        with pytest.raises(AuthError):
            tokens.verify("device-test-002", token_a)

    def test_last_used_is_recorded(self, tokens: TokenManager) -> None:
        _, token = tokens.enroll(DEVICE)
        assert tokens.tokens(DEVICE)[0]["last_used_utc"] is None
        tokens.verify(DEVICE, token)
        assert tokens.tokens(DEVICE)[0]["last_used_utc"] is not None

    def test_disabled_device_is_refused(
        self, tokens: TokenManager, database: Database
    ) -> None:
        _, token = tokens.enroll(DEVICE)
        database.set_device_enabled(DEVICE, False)
        with pytest.raises(AuthError) as exc:
            tokens.verify(DEVICE, token)
        assert exc.value.status == 403


class TestRevocationAndRotation:
    def test_revoked_token_stops_working(self, tokens: TokenManager) -> None:
        token_id, token = tokens.enroll(DEVICE)
        assert tokens.revoke(token_id) is True
        with pytest.raises(AuthError):
            tokens.verify(DEVICE, token)

    def test_revoking_twice_is_a_no_op(self, tokens: TokenManager) -> None:
        token_id, _ = tokens.enroll(DEVICE)
        assert tokens.revoke(token_id) is True
        assert tokens.revoke(token_id) is False

    def test_rotation_issues_a_new_token_and_kills_the_old(self, tokens: TokenManager) -> None:
        _, old = tokens.enroll(DEVICE)
        _, new = tokens.rotate(DEVICE)

        assert new != old
        assert tokens.verify(DEVICE, new)
        with pytest.raises(AuthError):
            tokens.verify(DEVICE, old)

    def test_revoke_all_disables_every_token(self, tokens: TokenManager) -> None:
        _, first = tokens.enroll(DEVICE)
        _, second = tokens.enroll(DEVICE)
        assert tokens.revoke_all(DEVICE) == 2
        for token in (first, second):
            with pytest.raises(AuthError):
                tokens.verify(DEVICE, token)


class TestRedaction:
    def test_token_value_never_appears(self) -> None:
        token = "atl_super_secret_value_abcdef"
        rendered = redact_token(token)
        assert token not in rendered
        assert "secret" not in rendered

    def test_absent_token(self) -> None:
        assert redact_token(None) == "<absent>"


class TestRateLimiter:
    def test_allows_up_to_the_limit(self) -> None:
        limiter = RateLimiter(limit=3, window_seconds=60)
        for index in range(3):
            limiter.check("device", now=float(index))

    def test_rejects_beyond_the_limit(self) -> None:
        limiter = RateLimiter(limit=2, window_seconds=60)
        limiter.check("device", now=0.0)
        limiter.check("device", now=1.0)
        with pytest.raises(AuthError) as exc:
            limiter.check("device", now=2.0)
        assert exc.value.status == 429

    def test_window_slides(self) -> None:
        limiter = RateLimiter(limit=1, window_seconds=10)
        limiter.check("device", now=0.0)
        with pytest.raises(AuthError):
            limiter.check("device", now=5.0)
        limiter.check("device", now=100.0)

    def test_keys_are_independent(self) -> None:
        limiter = RateLimiter(limit=1, window_seconds=60)
        limiter.check("device-a", now=0.0)
        limiter.check("device-b", now=0.0)
