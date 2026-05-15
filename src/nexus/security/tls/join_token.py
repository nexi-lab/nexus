"""K3s-style join token for cluster mTLS bootstrap (#2694).

A join token encodes a shared secret and the CA fingerprint so that a
joining node can authenticate to the leader and verify the CA it
receives in the JoinCluster response.

Token format::

    K10<password>::server:<ca_fingerprint>

Where:
    - ``K10`` is a version prefix (K3s convention)
    - ``<password>`` is a 64-char hex string (32 random bytes)
    - ``<ca_fingerprint>`` is ``SHA256:<base64>`` (same as ``cert_fingerprint``)

Usage::

    from nexus.security.tls.join_token import generate_join_token, parse_join_token

    # Leader side — generate token + hash
    token, pw_hash = generate_join_token(ca_cert)
    # write token to {data_dir}/tls/join-token
    # write pw_hash to {data_dir}/tls/join-token-hash

    # Joiner side — parse token from CLI flag
    password, expected_fp = parse_join_token(token_string)
    # use password in JoinCluster RPC, verify CA fingerprint after
"""

from __future__ import annotations

import hashlib
import secrets

from cryptography import x509

from nexus.security.tls.certgen import cert_fingerprint

TOKEN_PREFIX = "K10"
TOKEN_SEPARATOR = "::server:"


def generate_join_token(ca_cert: x509.Certificate) -> tuple[str, str]:
    """Generate a join token for cluster bootstrap.

    Args:
        ca_cert: The cluster CA certificate (used for fingerprint).

    Returns:
        Tuple of (token_string, password_sha256_hash).
        The token is given to operators; the hash is stored server-side
        for verification.
    """
    password = secrets.token_hex(32)  # 64-char hex string
    fp = cert_fingerprint(ca_cert)
    token = f"{TOKEN_PREFIX}{password}{TOKEN_SEPARATOR}{fp}"
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    return token, pw_hash


def parse_join_token(token: str) -> tuple[str, str]:
    """Parse a join token into its components.

    Args:
        token: Token string in ``K10<password>::server:<fingerprint>`` format.

    Returns:
        Tuple of (password, ca_fingerprint).

    Raises:
        ValueError: If the token format is invalid.
    """
    if not token.startswith(TOKEN_PREFIX):
        raise ValueError(f"Invalid join token: must start with '{TOKEN_PREFIX}'")

    body = token[len(TOKEN_PREFIX) :]
    if TOKEN_SEPARATOR not in body:
        raise ValueError(f"Invalid join token: missing '{TOKEN_SEPARATOR}' separator")

    password, ca_fingerprint = body.split(TOKEN_SEPARATOR, 1)

    if not password:
        raise ValueError("Invalid join token: empty password")
    if not ca_fingerprint.startswith("SHA256:"):
        raise ValueError("Invalid join token: fingerprint must start with 'SHA256:'")

    return password, ca_fingerprint


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a join password against the stored SHA-256 hash.

    Uses constant-time comparison to prevent timing attacks.

    Args:
        password: The plaintext password from the join token.
        stored_hash: The SHA-256 hex digest stored on the server.

    Returns:
        True if the password matches.
    """
    candidate = hashlib.sha256(password.encode()).hexdigest()
    return secrets.compare_digest(candidate, stored_hash)
