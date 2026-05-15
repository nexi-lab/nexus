"""Tests for OAuthCrypto's fail-loud policy on missing key persistence.

Historical behavior: with no explicit key, no wired settings_store, and
no ``NEXUS_OAUTH_ENCRYPTION_KEY`` env, ``OAuthCrypto()`` silently
generated an ephemeral Fernet key. Any OAuth token encrypted under
that key became undecryptable after the next restart — the root
cause of the post-R20.18.5 password_vault data-loss incident.

New behavior (this test file):

* Default: raise ``EphemeralOAuthKeyRefused``.
* Opt-in via ``NEXUS_ALLOW_EPHEMERAL_OAUTH_KEY=1``: allow ephemeral
  but log a loud warning. The opt-in exists for dev/test only.
* Explicit ``encryption_key=...`` still works unchanged.
* A valid ``settings_store`` path still works unchanged (load existing
  or create-and-store a new key).
"""

from __future__ import annotations

import pytest

from nexus.contracts.auth_store_types import SystemSettingDTO
from nexus.lib.oauth.crypto import (
    ALLOW_EPHEMERAL_KEY_ENV,
    OAUTH_ENCRYPTION_KEY_ENV,
    EphemeralOAuthKeyRefused,
    OAuthCrypto,
)


class _InMemorySettingsStore:
    """Minimal SystemSettingsStoreProtocol for unit tests."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[str, str | None]] = {}

    def get_setting(self, key: str) -> SystemSettingDTO | None:
        if key not in self._data:
            return None
        value, description = self._data[key]
        return SystemSettingDTO(key=key, value=value, description=description)

    def set_setting(self, key: str, value: str, *, description: str | None = None) -> None:
        self._data[key] = (value, description)


class TestFailLoudDefault:
    def test_no_inputs_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(OAUTH_ENCRYPTION_KEY_ENV, raising=False)
        monkeypatch.delenv(ALLOW_EPHEMERAL_KEY_ENV, raising=False)

        with pytest.raises(EphemeralOAuthKeyRefused):
            OAuthCrypto()

    def test_error_message_lists_all_resolution_paths(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(OAUTH_ENCRYPTION_KEY_ENV, raising=False)
        monkeypatch.delenv(ALLOW_EPHEMERAL_KEY_ENV, raising=False)

        with pytest.raises(EphemeralOAuthKeyRefused) as exc_info:
            OAuthCrypto()

        message = str(exc_info.value)
        assert "settings_store" in message
        assert OAUTH_ENCRYPTION_KEY_ENV in message
        assert ALLOW_EPHEMERAL_KEY_ENV in message


class TestOptInEphemeral:
    def test_env_opt_in_allows_ephemeral(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(OAUTH_ENCRYPTION_KEY_ENV, raising=False)
        monkeypatch.setenv(ALLOW_EPHEMERAL_KEY_ENV, "1")

        crypto = OAuthCrypto()

        # Smoke: can round-trip a token with the generated key.
        encrypted = crypto.encrypt_token("hello")
        assert crypto.decrypt_token(encrypted) == "hello"

    def test_env_set_to_non_1_still_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Only the literal ``1`` opts in — prevents typo / truthy surprises."""
        monkeypatch.delenv(OAUTH_ENCRYPTION_KEY_ENV, raising=False)
        monkeypatch.setenv(ALLOW_EPHEMERAL_KEY_ENV, "true")

        with pytest.raises(EphemeralOAuthKeyRefused):
            OAuthCrypto()


class TestPersistentPathsStillWork:
    def test_explicit_key_bypasses_fail_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(OAUTH_ENCRYPTION_KEY_ENV, raising=False)
        monkeypatch.delenv(ALLOW_EPHEMERAL_KEY_ENV, raising=False)

        key = OAuthCrypto.generate_key()
        crypto = OAuthCrypto(encryption_key=key)
        encrypted = crypto.encrypt_token("hello")
        assert crypto.decrypt_token(encrypted) == "hello"

    def test_env_key_bypasses_fail_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(OAUTH_ENCRYPTION_KEY_ENV, OAuthCrypto.generate_key())
        monkeypatch.delenv(ALLOW_EPHEMERAL_KEY_ENV, raising=False)

        crypto = OAuthCrypto()
        encrypted = crypto.encrypt_token("hello")
        assert crypto.decrypt_token(encrypted) == "hello"

    def test_settings_store_creates_key_on_first_use(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(OAUTH_ENCRYPTION_KEY_ENV, raising=False)
        monkeypatch.delenv(ALLOW_EPHEMERAL_KEY_ENV, raising=False)

        store = _InMemorySettingsStore()
        crypto1 = OAuthCrypto(settings_store=store)

        # Second instance sees persisted key → can decrypt first's ciphertext.
        ciphertext = crypto1.encrypt_token("hello")
        crypto2 = OAuthCrypto(settings_store=store)
        assert crypto2.decrypt_token(ciphertext) == "hello"

    def test_settings_store_key_survives_env_and_ephemeral_flags(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When a wired settings_store provides a key, env and ephemeral
        paths are irrelevant — the persistent key wins."""
        monkeypatch.setenv(OAUTH_ENCRYPTION_KEY_ENV, OAuthCrypto.generate_key())
        monkeypatch.setenv(ALLOW_EPHEMERAL_KEY_ENV, "1")

        store = _InMemorySettingsStore()
        crypto1 = OAuthCrypto(settings_store=store)
        ciphertext = crypto1.encrypt_token("hello")

        # Second instance without the env keys still decrypts via the store.
        monkeypatch.delenv(OAUTH_ENCRYPTION_KEY_ENV, raising=False)
        monkeypatch.delenv(ALLOW_EPHEMERAL_KEY_ENV, raising=False)
        crypto2 = OAuthCrypto(settings_store=store)
        assert crypto2.decrypt_token(ciphertext) == "hello"
