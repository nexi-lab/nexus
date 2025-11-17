"""Tests for OAuth encryption utilities."""

import pytest
from cryptography.fernet import InvalidToken

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
