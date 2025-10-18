"""AWS Signature Version 4 (SigV4) authentication for S3-compatible API.

This module implements AWS SigV4 authentication to secure the S3-compatible
server. It validates request signatures against configured access keys.

References:
- https://docs.aws.amazon.com/general/latest/gr/signature-version-4.html
- https://docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-authenticating-requests.html
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlparse


@dataclass
class Credentials:
    """AWS-style credentials for authentication."""

    access_key_id: str
    secret_access_key: str
    # Optional: Add support for session tokens later
    # session_token: str | None = None


class SigV4Validator:
    """AWS Signature Version 4 validator.

    Validates S3 request signatures against configured credentials.
    Supports both Authorization header and query string authentication.
    """

    def __init__(self, credentials_store: dict[str, Credentials]):
        """Initialize validator with credentials store.

        Args:
            credentials_store: Dict mapping access_key_id to Credentials
        """
        self.credentials_store = credentials_store

    def validate_request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: bytes = b"",
    ) -> tuple[bool, str | None]:
        """Validate AWS SigV4 signature for a request.

        Args:
            method: HTTP method (GET, PUT, POST, DELETE, etc.)
            url: Full request URL
            headers: Request headers (case-insensitive)
            payload: Request body bytes

        Returns:
            Tuple of (is_valid, error_message)
            - (True, None) if signature is valid
            - (False, error_msg) if signature is invalid
        """
        # Normalize headers to lowercase for case-insensitive lookup
        headers_lower = {k.lower(): v for k, v in headers.items()}

        # Check for Authorization header
        auth_header = headers_lower.get("authorization")
        if not auth_header:
            return False, "Missing Authorization header"

        # Parse Authorization header
        # Format: AWS4-HMAC-SHA256 Credential=access_key/date/region/service/aws4_request,
        #         SignedHeaders=header1;header2, Signature=signature
        if not auth_header.startswith("AWS4-HMAC-SHA256"):
            return False, "Invalid authorization scheme (expected AWS4-HMAC-SHA256)"

        try:
            parts = auth_header.split(",")
            credential_part = None
            signed_headers_part = None
            signature_part = None

            for part in parts:
                part = part.strip()
                if "Credential=" in part:
                    credential_part = part.split("Credential=")[1]
                elif "SignedHeaders=" in part:
                    signed_headers_part = part.split("SignedHeaders=")[1]
                elif "Signature=" in part:
                    signature_part = part.split("Signature=")[1]

            if not all([credential_part, signed_headers_part, signature_part]):
                return False, "Malformed Authorization header"

            # Parse credential
            # Format: access_key/YYYYMMDD/region/s3/aws4_request
            credential_parts = credential_part.split("/")
            if len(credential_parts) < 5:
                return False, "Invalid credential format"

            access_key = credential_parts[0]
            date_stamp = credential_parts[1]
            region = credential_parts[2]
            service = credential_parts[3]
            request_type = credential_parts[4]

            if service != "s3":
                return False, f"Invalid service in credential (expected s3, got {service})"

            if request_type != "aws4_request":
                return False, "Invalid request type in credential"

            # Look up credentials
            credentials = self.credentials_store.get(access_key)
            if not credentials:
                return False, f"Unknown access key: {access_key}"

            # Parse signed headers
            signed_headers = signed_headers_part.split(";")

            # Get timestamp from x-amz-date header
            amz_date = headers_lower.get("x-amz-date")
            if not amz_date:
                return False, "Missing x-amz-date header"

            # Compute expected signature
            expected_signature = self._compute_signature(
                method=method,
                url=url,
                headers=headers_lower,
                signed_headers=signed_headers,
                payload=payload,
                amz_date=amz_date,
                date_stamp=date_stamp,
                region=region,
                service=service,
                secret_key=credentials.secret_access_key,
            )

            # Compare signatures (constant-time comparison)
            if hmac.compare_digest(signature_part, expected_signature):
                return True, None
            else:
                return False, "Signature mismatch"

        except Exception as e:
            return False, f"Signature validation error: {e}"

    def _compute_signature(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        signed_headers: list[str],
        payload: bytes,
        amz_date: str,
        date_stamp: str,
        region: str,
        service: str,
        secret_key: str,
    ) -> str:
        """Compute AWS SigV4 signature.

        Args:
            method: HTTP method
            url: Request URL
            headers: Request headers (lowercase)
            signed_headers: List of signed header names
            payload: Request body
            amz_date: ISO8601 timestamp
            date_stamp: YYYYMMDD date stamp
            region: AWS region
            service: AWS service (s3)
            secret_key: Secret access key

        Returns:
            Hex-encoded signature string
        """
        # Step 1: Create canonical request
        canonical_request = self._create_canonical_request(
            method, url, headers, signed_headers, payload
        )

        # Debug logging (disabled in production)
        # import logging
        # logger = logging.getLogger(__name__)
        # logger.debug(f"Canonical request:\n{canonical_request}")

        # Step 2: Create string to sign
        algorithm = "AWS4-HMAC-SHA256"
        credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
        string_to_sign = "\n".join(
            [
                algorithm,
                amz_date,
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )

        # Step 3: Calculate signature
        signing_key = self._get_signature_key(secret_key, date_stamp, region, service)
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        return signature

    def _create_canonical_request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        signed_headers: list[str],
        payload: bytes,
    ) -> str:
        """Create canonical request for SigV4.

        Args:
            method: HTTP method
            url: Request URL
            headers: Request headers (lowercase)
            signed_headers: List of signed header names
            payload: Request body

        Returns:
            Canonical request string
        """
        # Parse URL
        parsed = urlparse(url)
        canonical_uri = parsed.path or "/"

        # Canonical query string
        if parsed.query:
            # Parse and sort query parameters
            # AWS SigV4 requires: name=value pairs, sorted by name, URL-encoded
            # Per RFC 3986, unreserved characters (A-Z, a-z, 0-9, -, _, ., ~) should not be encoded
            from urllib.parse import parse_qsl
            query_params = parse_qsl(parsed.query, keep_blank_values=True)
            # Sort by parameter name
            query_params.sort()
            # URL encode and join (preserve unreserved characters)
            canonical_query_string = "&".join(
                f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in query_params
            )
        else:
            canonical_query_string = ""

        # Canonical headers
        canonical_headers_list = []
        for header_name in sorted(signed_headers):
            header_value = headers.get(header_name, "")
            # Trim whitespace and convert to lowercase
            canonical_headers_list.append(f"{header_name}:{header_value.strip()}")
        canonical_headers = "\n".join(canonical_headers_list) + "\n"

        # Signed headers list
        signed_headers_str = ";".join(sorted(signed_headers))

        # Payload hash
        # Check if client sent x-amz-content-sha256 header indicating unsigned payload
        content_sha256 = headers.get("x-amz-content-sha256", "")
        if content_sha256 == "UNSIGNED-PAYLOAD":
            # S3 allows unsigned payload with streaming uploads
            payload_hash = "UNSIGNED-PAYLOAD"
        else:
            # Use actual payload hash
            payload_hash = hashlib.sha256(payload).hexdigest()

        # Canonical request
        canonical_request = "\n".join(
            [
                method,
                canonical_uri,
                canonical_query_string,
                canonical_headers,
                signed_headers_str,
                payload_hash,
            ]
        )

        return canonical_request

    def _get_signature_key(self, key: str, date_stamp: str, region: str, service: str) -> bytes:
        """Derive signing key from secret key.

        Args:
            key: Secret access key
            date_stamp: YYYYMMDD date stamp
            region: AWS region
            service: AWS service (s3)

        Returns:
            Signing key bytes
        """
        k_date = hmac.new(f"AWS4{key}".encode("utf-8"), date_stamp.encode("utf-8"), hashlib.sha256).digest()
        k_region = hmac.new(k_date, region.encode("utf-8"), hashlib.sha256).digest()
        k_service = hmac.new(k_region, service.encode("utf-8"), hashlib.sha256).digest()
        k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
        return k_signing


def create_simple_credentials_store(
    access_key: str, secret_key: str
) -> dict[str, Credentials]:
    """Create a simple in-memory credentials store.

    Args:
        access_key: AWS access key ID
        secret_key: AWS secret access key

    Returns:
        Credentials store mapping access_key to Credentials
    """
    return {access_key: Credentials(access_key_id=access_key, secret_access_key=secret_key)}
