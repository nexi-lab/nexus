"""Unit tests for AWS SigV4 authentication."""

import hashlib
import hmac
from datetime import datetime

import pytest

from nexus.server.auth import Credentials, SigV4Validator, create_simple_credentials_store


class TestCredentials:
    """Test Credentials dataclass."""

    def test_credentials_creation(self):
        """Test creating credentials."""
        creds = Credentials(
            access_key_id="test-key",
            secret_access_key="test-secret",
        )
        assert creds.access_key_id == "test-key"
        assert creds.secret_access_key == "test-secret"


class TestSigV4Validator:
    """Test AWS SigV4 signature validation."""

    def setup_method(self):
        """Set up test fixtures."""
        self.access_key = "test-access-key"
        self.secret_key = "test-secret-key"
        self.credentials_store = create_simple_credentials_store(
            self.access_key, self.secret_key
        )
        self.validator = SigV4Validator(self.credentials_store)

    def test_create_simple_credentials_store(self):
        """Test creating a simple credentials store."""
        store = create_simple_credentials_store("key1", "secret1")
        assert "key1" in store
        assert store["key1"].access_key_id == "key1"
        assert store["key1"].secret_access_key == "secret1"

    def test_validate_request_missing_authorization(self):
        """Test validation fails with missing authorization header."""
        valid, error = self.validator.validate_request(
            method="GET",
            url="http://localhost:8080/nexus",
            headers={},
            payload=b"",
        )
        assert valid is False
        assert "Missing Authorization header" in error

    def test_validate_request_invalid_scheme(self):
        """Test validation fails with invalid authorization scheme."""
        valid, error = self.validator.validate_request(
            method="GET",
            url="http://localhost:8080/nexus",
            headers={"authorization": "Bearer token123"},
            payload=b"",
        )
        assert valid is False
        assert "Invalid authorization scheme" in error

    def test_validate_request_malformed_header(self):
        """Test validation fails with malformed authorization header."""
        valid, error = self.validator.validate_request(
            method="GET",
            url="http://localhost:8080/nexus",
            headers={"authorization": "AWS4-HMAC-SHA256 invalid"},
            payload=b"",
        )
        assert valid is False
        assert "Malformed Authorization header" in error

    def test_validate_request_invalid_credential_format(self):
        """Test validation fails with invalid credential format."""
        valid, error = self.validator.validate_request(
            method="GET",
            url="http://localhost:8080/nexus",
            headers={
                "authorization": (
                    "AWS4-HMAC-SHA256 Credential=short, "
                    "SignedHeaders=host;x-amz-date, "
                    "Signature=abc123"
                ),
                "x-amz-date": "20251018T080000Z",
            },
            payload=b"",
        )
        assert valid is False
        assert "Invalid credential format" in error

    def test_validate_request_unknown_access_key(self):
        """Test validation fails with unknown access key."""
        valid, error = self.validator.validate_request(
            method="GET",
            url="http://localhost:8080/nexus",
            headers={
                "authorization": (
                    "AWS4-HMAC-SHA256 "
                    "Credential=unknown-key/20251018/us-east-1/s3/aws4_request, "
                    "SignedHeaders=host;x-amz-date, "
                    "Signature=abc123"
                ),
                "x-amz-date": "20251018T080000Z",
                "host": "localhost:8080",
            },
            payload=b"",
        )
        assert valid is False
        assert "Unknown access key" in error

    def test_validate_request_missing_amz_date(self):
        """Test validation fails with missing x-amz-date header."""
        valid, error = self.validator.validate_request(
            method="GET",
            url="http://localhost:8080/nexus",
            headers={
                "authorization": (
                    f"AWS4-HMAC-SHA256 "
                    f"Credential={self.access_key}/20251018/us-east-1/s3/aws4_request, "
                    f"SignedHeaders=host, "
                    f"Signature=abc123"
                ),
                "host": "localhost:8080",
            },
            payload=b"",
        )
        assert valid is False
        assert "Missing x-amz-date header" in error

    def test_validate_request_invalid_service(self):
        """Test validation fails with invalid service in credential."""
        valid, error = self.validator.validate_request(
            method="GET",
            url="http://localhost:8080/nexus",
            headers={
                "authorization": (
                    f"AWS4-HMAC-SHA256 "
                    f"Credential={self.access_key}/20251018/us-east-1/ec2/aws4_request, "
                    f"SignedHeaders=host;x-amz-date, "
                    f"Signature=abc123"
                ),
                "x-amz-date": "20251018T080000Z",
                "host": "localhost:8080",
            },
            payload=b"",
        )
        assert valid is False
        assert "Invalid service" in error

    def test_canonical_query_string_encoding(self):
        """Test canonical query string encoding."""
        # Create a request with query parameters
        url = "http://localhost:8080/nexus/file.txt?x-id=PutObject&key=value"

        # The validator should properly encode query parameters
        # This is tested indirectly through signature validation
        valid, error = self.validator.validate_request(
            method="GET",
            url=url,
            headers={
                "authorization": (
                    f"AWS4-HMAC-SHA256 "
                    f"Credential={self.access_key}/20251018/us-east-1/s3/aws4_request, "
                    f"SignedHeaders=host;x-amz-date, "
                    f"Signature=incorrect"
                ),
                "x-amz-date": "20251018T080000Z",
                "host": "localhost:8080",
            },
            payload=b"",
        )
        # Should fail with signature mismatch, not encoding error
        assert valid is False
        assert "Signature mismatch" in error

    def test_unsigned_payload_handling(self):
        """Test UNSIGNED-PAYLOAD handling in signature."""
        # This tests that UNSIGNED-PAYLOAD is properly handled
        # The actual signature will be wrong, but we test that it doesn't crash
        valid, error = self.validator.validate_request(
            method="PUT",
            url="http://localhost:8080/nexus/file.txt",
            headers={
                "authorization": (
                    f"AWS4-HMAC-SHA256 "
                    f"Credential={self.access_key}/20251018/us-east-1/s3/aws4_request, "
                    f"SignedHeaders=host;x-amz-content-sha256;x-amz-date, "
                    f"Signature=incorrect"
                ),
                "x-amz-date": "20251018T080000Z",
                "x-amz-content-sha256": "UNSIGNED-PAYLOAD",
                "host": "localhost:8080",
            },
            payload=b"test content",
        )
        # Should fail with signature mismatch, not crash
        assert valid is False
        assert "Signature mismatch" in error

    def test_get_signature_key(self):
        """Test signature key derivation."""
        date_stamp = "20251018"
        region = "us-east-1"
        service = "s3"

        # Access the private method for testing
        signing_key = self.validator._get_signature_key(
            self.secret_key, date_stamp, region, service
        )

        assert isinstance(signing_key, bytes)
        assert len(signing_key) == 32  # SHA256 produces 32 bytes


class TestSignatureComputation:
    """Test AWS SigV4 signature computation."""

    def setup_method(self):
        """Set up test fixtures."""
        self.access_key = "AKIAIOSFODNN7EXAMPLE"
        self.secret_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        self.credentials_store = {
            self.access_key: Credentials(self.access_key, self.secret_key)
        }
        self.validator = SigV4Validator(self.credentials_store)

    def test_canonical_request_format(self):
        """Test canonical request format."""
        # Test that canonical request is properly formatted
        method = "GET"
        url = "http://localhost:8080/nexus/test.txt"
        headers = {
            "host": "localhost:8080",
            "x-amz-date": "20251018T120000Z",
        }
        signed_headers = ["host", "x-amz-date"]
        payload = b""

        canonical_request = self.validator._create_canonical_request(
            method, url, headers, signed_headers, payload
        )

        # Canonical request should have the format:
        # METHOD\nURI\nQUERY_STRING\nHEADERS\nSIGNED_HEADERS\nPAYLOAD_HASH
        lines = canonical_request.split("\n")
        assert lines[0] == "GET"
        assert lines[1] == "/nexus/test.txt"
        assert lines[2] == ""  # No query string
        assert "host:localhost:8080" in canonical_request
        assert "x-amz-date:20251018T120000Z" in canonical_request

    def test_canonical_request_with_query_string(self):
        """Test canonical request with query parameters."""
        method = "GET"
        url = "http://localhost:8080/nexus/?list-type=2&max-keys=100"
        headers = {
            "host": "localhost:8080",
            "x-amz-date": "20251018T120000Z",
        }
        signed_headers = ["host", "x-amz-date"]
        payload = b""

        canonical_request = self.validator._create_canonical_request(
            method, url, headers, signed_headers, payload
        )

        # Query string should be properly encoded and sorted
        assert "list-type=2" in canonical_request
        assert "max-keys=100" in canonical_request

    def test_canonical_request_with_unsigned_payload(self):
        """Test canonical request with UNSIGNED-PAYLOAD."""
        method = "PUT"
        url = "http://localhost:8080/nexus/file.txt"
        headers = {
            "host": "localhost:8080",
            "x-amz-date": "20251018T120000Z",
            "x-amz-content-sha256": "UNSIGNED-PAYLOAD",
        }
        signed_headers = ["host", "x-amz-content-sha256", "x-amz-date"]
        payload = b"test content"

        canonical_request = self.validator._create_canonical_request(
            method, url, headers, signed_headers, payload
        )

        # Should use UNSIGNED-PAYLOAD instead of actual hash
        assert canonical_request.endswith("UNSIGNED-PAYLOAD")

    def test_canonical_request_with_signed_payload(self):
        """Test canonical request with signed payload."""
        method = "PUT"
        url = "http://localhost:8080/nexus/file.txt"
        payload = b"test content"
        expected_hash = hashlib.sha256(payload).hexdigest()

        headers = {
            "host": "localhost:8080",
            "x-amz-date": "20251018T120000Z",
        }
        signed_headers = ["host", "x-amz-date"]

        canonical_request = self.validator._create_canonical_request(
            method, url, headers, signed_headers, payload
        )

        # Should use actual payload hash
        assert canonical_request.endswith(expected_hash)
