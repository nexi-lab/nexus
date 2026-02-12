"""Tests for OAuth encryption utilities."""

import base64
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet, InvalidToken

from nexus.server.auth.oauth_crypto import OAuthCrypto


class TestOAuthCrypto:
    """Test suite for OAuthCrypto encryption utilities."""

    def test_generate_key(self):
        """Test key generation."""
        key = OAuthCrypto.generate_key()
        assert isinstance(key, str)
        assert len(key) > 0

        # Should be able to create crypto instance with generated key
        crypto = OAuthCrypto(key)
        assert crypto is not None

    def test_encrypt_decrypt_token(self):
        """Test basic token encryption and decryption."""
        crypto = OAuthCrypto()
        token = "ya29.a0ARrdaM_test_token_1234567890"

        # Encrypt
        encrypted = crypto.encrypt_token(token)
        assert isinstance(encrypted, str)
        assert encrypted != token  # Should be different from original

        # Decrypt
        decrypted = crypto.decrypt_token(encrypted)
        assert decrypted == token  # Should match original

    def test_encrypt_empty_token_fails(self):
        """Test that encrypting empty token raises error."""
        crypto = OAuthCrypto()

        with pytest.raises(ValueError, match="Token cannot be empty"):
            crypto.encrypt_token("")

    def test_decrypt_empty_token_fails(self):
        """Test that decrypting empty token raises error."""
        crypto = OAuthCrypto()

        with pytest.raises(ValueError, match="Encrypted token cannot be empty"):
            crypto.decrypt_token("")

    def test_decrypt_invalid_token_fails(self):
        """Test that decrypting invalid token raises error."""
        crypto = OAuthCrypto()

        with pytest.raises(InvalidToken):
            crypto.decrypt_token("invalid_encrypted_token")

    def test_decrypt_with_wrong_key_fails(self):
        """Test that decrypting with wrong key fails."""
        # Generate two different keys explicitly
        key1 = OAuthCrypto.generate_key()
        key2 = OAuthCrypto.generate_key()

        crypto1 = OAuthCrypto(key1)
        crypto2 = OAuthCrypto(key2)  # Different key

        token = "test_token_123"
        encrypted = crypto1.encrypt_token(token)

        # Should fail to decrypt with different key
        with pytest.raises(InvalidToken):
            crypto2.decrypt_token(encrypted)

    def test_encrypt_decrypt_dict(self):
        """Test dictionary encryption and decryption."""
        crypto = OAuthCrypto()
        data = {
            "access_token": "ya29.a0ARrdaM_test",
            "refresh_token": "1//0e_test",
            "expires_in": 3600,
            "scopes": ["https://www.googleapis.com/auth/drive"],
        }

        # Encrypt
        encrypted = crypto.encrypt_dict(data)
        assert isinstance(encrypted, str)

        # Decrypt
        decrypted = crypto.decrypt_dict(encrypted)
        assert decrypted == data

    def test_encrypt_empty_dict_fails(self):
        """Test that encrypting empty dict raises error."""
        crypto = OAuthCrypto()

        with pytest.raises(ValueError, match="Data cannot be empty"):
            crypto.encrypt_dict({})

    def test_decrypt_invalid_dict_fails(self):
        """Test that decrypting invalid dict raises error."""
        crypto = OAuthCrypto()

        with pytest.raises(InvalidToken):
            crypto.decrypt_dict("invalid_encrypted_data")

    def test_rotate_key(self):
        """Test key rotation."""
        old_key = OAuthCrypto.generate_key()
        new_key = OAuthCrypto.generate_key()

        old_crypto = OAuthCrypto(old_key)
        token = "test_token_for_rotation"

        # Encrypt with old key
        old_encrypted = old_crypto.encrypt_token(token)

        # Rotate to new key
        new_encrypted = old_crypto.rotate_key(old_key, new_key, old_encrypted)

        # Should be able to decrypt with new key
        new_crypto = OAuthCrypto(new_key)
        decrypted = new_crypto.decrypt_token(new_encrypted)
        assert decrypted == token

        # Should NOT be able to decrypt with old key
        with pytest.raises(InvalidToken):
            old_crypto.decrypt_token(new_encrypted)

    def test_multiple_encryptions_produce_different_ciphertexts(self):
        """Test that encrypting the same token multiple times produces different ciphertexts.

        This is expected behavior for Fernet (includes timestamp and IV).
        """
        crypto = OAuthCrypto()
        token = "test_token_123"

        encrypted1 = crypto.encrypt_token(token)
        encrypted2 = crypto.encrypt_token(token)

        # Different ciphertexts
        assert encrypted1 != encrypted2

        # But both decrypt to same plaintext
        assert crypto.decrypt_token(encrypted1) == token
        assert crypto.decrypt_token(encrypted2) == token

    def test_long_token_encryption(self):
        """Test encryption of long tokens (realistic scenario)."""
        crypto = OAuthCrypto()

        # Simulate a realistic Google access token (1000+ chars)
        token = "ya29.a0ARrdaM" + "x" * 1000

        encrypted = crypto.encrypt_token(token)
        decrypted = crypto.decrypt_token(encrypted)

        assert decrypted == token

    def test_special_characters_in_token(self):
        """Test encryption of tokens with special characters."""
        crypto = OAuthCrypto()

        # Token with various special characters
        token = "token_with_special!@#$%^&*()_+-=[]{}|;':\",./<>?`~"

        encrypted = crypto.encrypt_token(token)
        decrypted = crypto.decrypt_token(encrypted)

        assert decrypted == token

    def test_unicode_in_token(self):
        """Test encryption of tokens with unicode characters."""
        crypto = OAuthCrypto()

        # Token with unicode
        token = "token_with_unicode_测试_🔒_Ñoño"

        encrypted = crypto.encrypt_token(token)
        decrypted = crypto.decrypt_token(encrypted)

        assert decrypted == token


class TestOAuthCryptoInit:
    """Tests for OAuthCrypto.__init__ key-loading priority chain.

    The key-loading order is:
    1. Explicit encryption_key parameter
    2. NEXUS_OAUTH_ENCRYPTION_KEY environment variable
    3. Database (via _load_or_create_key_from_db)
    4. Random key (not persistent — logs warning)
    """

    def test_init_with_explicit_key(self, monkeypatch):
        """Explicit key takes priority over env var."""
        explicit_key = Fernet.generate_key().decode("utf-8")
        env_key = Fernet.generate_key().decode("utf-8")

        # Set env var — it should be ignored when explicit key is provided
        monkeypatch.setenv("NEXUS_OAUTH_ENCRYPTION_KEY", env_key)

        crypto = OAuthCrypto(encryption_key=explicit_key)

        # Verify it uses the explicit key, not the env var
        token = "priority_test_token"
        encrypted = crypto.encrypt_token(token)

        # Decrypt with explicit key should work
        explicit_crypto = OAuthCrypto(encryption_key=explicit_key)
        assert explicit_crypto.decrypt_token(encrypted) == token

        # Decrypt with env key should fail
        env_crypto = OAuthCrypto(encryption_key=env_key)
        with pytest.raises(InvalidToken):
            env_crypto.decrypt_token(encrypted)

    def test_init_from_env_var(self, monkeypatch):
        """Env var is used when no explicit key is provided."""
        env_key = Fernet.generate_key().decode("utf-8")
        monkeypatch.setenv("NEXUS_OAUTH_ENCRYPTION_KEY", env_key)

        crypto = OAuthCrypto()

        # Verify it uses the env var key
        token = "env_var_test_token"
        encrypted = crypto.encrypt_token(token)

        env_crypto = OAuthCrypto(encryption_key=env_key)
        assert env_crypto.decrypt_token(encrypted) == token

    def test_init_env_var_whitespace_only_ignored(self, monkeypatch):
        """Whitespace-only env var falls through to random key (no crash)."""
        monkeypatch.setenv("NEXUS_OAUTH_ENCRYPTION_KEY", "   ")

        # Should not crash — falls through to random key generation
        crypto = OAuthCrypto()

        # Still functional with a random key
        token = "whitespace_env_test"
        encrypted = crypto.encrypt_token(token)
        assert crypto.decrypt_token(encrypted) == token

    def test_init_from_db_key(self, monkeypatch):
        """DB key is used when no explicit key or env var is provided."""
        db_key = Fernet.generate_key().decode("utf-8")
        monkeypatch.delenv("NEXUS_OAUTH_ENCRYPTION_KEY", raising=False)

        with patch.object(OAuthCrypto, "_load_or_create_key_from_db", return_value=db_key):
            crypto = OAuthCrypto(db_url="sqlite:///fake.db")

        # Verify it uses the DB key
        token = "db_key_test_token"
        encrypted = crypto.encrypt_token(token)

        db_crypto = OAuthCrypto(encryption_key=db_key)
        assert db_crypto.decrypt_token(encrypted) == token

    def test_init_db_failure_falls_back_to_random(self, monkeypatch, caplog):
        """When DB returns None, falls back to random key with warning.

        NOTE: This is a silent-swallow risk — if DB fails, tokens encrypted
        with the random key will be unreadable after restart. The current
        behavior logs a warning but does not raise an error.
        """
        monkeypatch.delenv("NEXUS_OAUTH_ENCRYPTION_KEY", raising=False)

        with (
            patch.object(OAuthCrypto, "_load_or_create_key_from_db", return_value=None),
            caplog.at_level(logging.WARNING),
        ):
            crypto = OAuthCrypto(db_url="sqlite:///fake.db")

        # Should still work with a random key
        token = "db_failure_fallback_test"
        encrypted = crypto.encrypt_token(token)
        assert crypto.decrypt_token(encrypted) == token

        # Should have logged a warning about non-persistent key
        assert any("NOT persist" in msg for msg in caplog.messages)

    def test_init_random_key_logs_warning(self, monkeypatch, caplog):
        """No key, no env, no db_url → random key with warning."""
        monkeypatch.delenv("NEXUS_OAUTH_ENCRYPTION_KEY", raising=False)

        with caplog.at_level(logging.WARNING):
            crypto = OAuthCrypto()

        # Should still be functional
        token = "random_key_test"
        encrypted = crypto.encrypt_token(token)
        assert crypto.decrypt_token(encrypted) == token

        # Should warn about non-persistent key
        assert any("NOT persist" in msg for msg in caplog.messages)

    def test_init_invalid_key_raises_valueerror(self):
        """Invalid Fernet key raises ValueError."""
        with pytest.raises(ValueError, match="Invalid encryption key"):
            OAuthCrypto(encryption_key="not-a-valid-fernet-key")


class TestOAuthCryptoTampering:
    """Tests for tamper detection in encrypted tokens."""

    def test_tampered_ciphertext_detected(self):
        """Flipping a byte in ciphertext is detected on decrypt."""
        crypto = OAuthCrypto()
        encrypted = crypto.encrypt_token("sensitive_token")

        # Decode base64, flip a byte in the middle, re-encode
        raw = base64.urlsafe_b64decode(encrypted)
        tampered = bytearray(raw)
        mid = len(tampered) // 2
        tampered[mid] ^= 0xFF  # flip all bits of one byte
        tampered_token = base64.urlsafe_b64encode(bytes(tampered)).decode("utf-8")

        with pytest.raises(InvalidToken):
            crypto.decrypt_token(tampered_token)

    def test_truncated_ciphertext_detected(self):
        """Truncated ciphertext is detected on decrypt."""
        crypto = OAuthCrypto()
        encrypted = crypto.encrypt_token("sensitive_token")

        truncated = encrypted[: len(encrypted) // 2]

        with pytest.raises(InvalidToken):
            crypto.decrypt_token(truncated)


class TestOAuthCryptoEdgeCases:
    """Tests for edge cases and error handling."""

    def test_encrypt_token_none_raises(self):
        """Passing None to encrypt_token raises ValueError."""
        crypto = OAuthCrypto()

        with pytest.raises(ValueError, match="Token cannot be empty"):
            crypto.encrypt_token(None)

    def test_decrypt_token_none_raises(self):
        """Passing None to decrypt_token raises ValueError."""
        crypto = OAuthCrypto()

        with pytest.raises(ValueError, match="Encrypted token cannot be empty"):
            crypto.decrypt_token(None)

    def test_encrypt_dict_non_serializable_raises(self):
        """Non-JSON-serializable dict raises ValueError."""
        crypto = OAuthCrypto()

        with pytest.raises(ValueError, match="Failed to serialize data"):
            crypto.encrypt_dict({"key": {1, 2, 3}})  # set is not JSON-serializable

    def test_decrypt_dict_non_json_content(self):
        """Encrypted non-JSON string raises ValueError on decrypt_dict."""
        crypto = OAuthCrypto()

        # Encrypt a plain string (not JSON)
        encrypted_non_json = crypto.encrypt_token("not json at all")

        with pytest.raises(ValueError, match="not valid JSON"):
            crypto.decrypt_dict(encrypted_non_json)


class TestOAuthCryptoConcurrency:
    """Tests for thread safety."""

    def test_concurrent_encrypt_decrypt(self):
        """Single crypto instance handles concurrent encrypt/decrypt safely."""
        crypto = OAuthCrypto()
        num_workers = 10

        def encrypt_decrypt_roundtrip(worker_id: int) -> bool:
            token = f"concurrent_token_{worker_id}"
            encrypted = crypto.encrypt_token(token)
            decrypted = crypto.decrypt_token(encrypted)
            return decrypted == token

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(encrypt_decrypt_roundtrip, i) for i in range(num_workers)]
            results = [f.result() for f in as_completed(futures)]

        assert all(results)
        assert len(results) == num_workers


class TestOAuthCryptoKeyRotation:
    """Extended tests for key rotation and key generation."""

    def test_rotate_key_preserves_dict_data(self):
        """Key rotation preserves dict data through encrypt_dict/decrypt_dict."""
        old_key = OAuthCrypto.generate_key()
        new_key = OAuthCrypto.generate_key()

        old_crypto = OAuthCrypto(encryption_key=old_key)
        data = {"access_token": "ya29.test", "refresh_token": "1//0e_test", "n": 42}

        # Encrypt dict with old key
        encrypted = old_crypto.encrypt_dict(data)

        # Rotate the underlying encrypted token
        rotated = old_crypto.rotate_key(old_key, new_key, encrypted)

        # Decrypt dict with new key
        new_crypto = OAuthCrypto(encryption_key=new_key)
        decrypted = new_crypto.decrypt_dict(rotated)

        assert decrypted == data

    def test_rotate_key_with_invalid_old_key_raises(self):
        """Rotation with wrong old key raises InvalidToken."""
        real_key = OAuthCrypto.generate_key()
        wrong_key = OAuthCrypto.generate_key()
        new_key = OAuthCrypto.generate_key()

        crypto = OAuthCrypto(encryption_key=real_key)
        encrypted = crypto.encrypt_token("token_to_rotate")

        with pytest.raises(InvalidToken):
            crypto.rotate_key(wrong_key, new_key, encrypted)

    def test_generate_key_uniqueness(self):
        """100 generated keys are all unique."""
        keys = [OAuthCrypto.generate_key() for _ in range(100)]
        assert len(set(keys)) == 100


class TestOAuthCryptoDbKeyLoading:
    """Integration tests for _load_or_create_key_from_db using SQLite in-memory."""

    def test_load_or_create_generates_new_key(self, monkeypatch):
        """First call with empty DB generates and stores a new key."""
        monkeypatch.delenv("NEXUS_OAUTH_ENCRYPTION_KEY", raising=False)

        crypto = OAuthCrypto(db_url="sqlite:///:memory:")

        # Should be functional — key was generated and stored
        token = "db_generated_key_test"
        encrypted = crypto.encrypt_token(token)
        assert crypto.decrypt_token(encrypted) == token

    def test_load_or_create_reuses_existing_key(self, monkeypatch, tmp_path):
        """Second instance with same DB file reuses the stored key."""
        monkeypatch.delenv("NEXUS_OAUTH_ENCRYPTION_KEY", raising=False)
        db_path = tmp_path / "test_keys.db"
        db_url = f"sqlite:///{db_path}"

        # First instance: generates and stores key
        crypto1 = OAuthCrypto(db_url=db_url)
        token = "reuse_key_test"
        encrypted = crypto1.encrypt_token(token)

        # Second instance: should load same key from DB
        crypto2 = OAuthCrypto(db_url=db_url)
        assert crypto2.decrypt_token(encrypted) == token

    def test_load_or_create_invalid_db_url_falls_back(self, monkeypatch, caplog):
        """Invalid DB URL falls back to random key with warning."""
        monkeypatch.delenv("NEXUS_OAUTH_ENCRYPTION_KEY", raising=False)

        with caplog.at_level(logging.WARNING):
            crypto = OAuthCrypto(db_url="invalid://not-a-real-db")

        # Should still work with random fallback key
        token = "invalid_db_fallback_test"
        encrypted = crypto.encrypt_token(token)
        assert crypto.decrypt_token(encrypted) == token

        # Should warn about both DB failure and non-persistent key
        assert any("NOT persist" in msg for msg in caplog.messages)

    def test_load_or_create_sqlite_check_same_thread(self, monkeypatch):
        """SQLite URLs get check_same_thread=False connect arg."""
        monkeypatch.delenv("NEXUS_OAUTH_ENCRYPTION_KEY", raising=False)

        # This exercises the "sqlite" in db_url branch (line 136-137)
        crypto = OAuthCrypto(db_url="sqlite:///:memory:")
        assert crypto.encrypt_token("thread_safe_test") is not None


class TestOAuthCryptoMiscCoverage:
    """Additional tests to cover remaining edge-case branches."""

    def test_decrypt_dict_empty_string_raises(self):
        """Empty string to decrypt_dict raises ValueError."""
        crypto = OAuthCrypto()

        with pytest.raises(ValueError, match="Encrypted data cannot be empty"):
            crypto.decrypt_dict("")
