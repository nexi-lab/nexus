"""Tests for RFC 7636 PKCE helpers."""

import base64
import hashlib

from nexus.lib.oauth.pkce import generate_pkce_pair, make_code_challenge


def test_generate_pkce_pair_returns_verifier_and_challenge() -> None:
    verifier, challenge = generate_pkce_pair()
    assert isinstance(verifier, str)
    assert isinstance(challenge, str)
    assert len(verifier) >= 43  # RFC 7636 §4.1 minimum
    assert len(verifier) <= 128  # RFC 7636 §4.1 maximum


def test_code_challenge_matches_spec() -> None:
    # RFC 7636 appendix B test vector.
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    expected = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert make_code_challenge(verifier) == expected


def test_challenge_is_sha256_urlsafe_b64_no_padding() -> None:
    verifier, challenge = generate_pkce_pair()
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    assert challenge == expected


def test_pairs_are_unique() -> None:
    pairs = {generate_pkce_pair() for _ in range(20)}
    assert len(pairs) == 20
