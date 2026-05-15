from __future__ import annotations

import os
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from nexus.bricks.auth.daemon.jwt_cache import FileJwtCache
from nexus.bricks.auth.daemon.jwt_client import JwtClient, JwtClientError
from nexus.server.api.v1.jwt_signer import DaemonClaims, JwtSigner

ClientSetup = tuple[JwtClient, JwtSigner, uuid.UUID, uuid.UUID, uuid.UUID]


@pytest.fixture
def server_signer() -> JwtSigner:
    k = ec.generate_private_key(ec.SECP256R1())
    pem = k.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return JwtSigner.from_pem(pem, issuer="https://test.nexus")


@pytest.fixture
def client_setup(tmp_path: Path, server_signer: JwtSigner) -> ClientSetup:
    priv = ed25519.Ed25519PrivateKey.generate()
    key_path = tmp_path / "machine.key"
    key_path.write_bytes(
        priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    os.chmod(key_path, 0o600)
    jwt_cache = tmp_path / "jwt.cache"
    pub_path = tmp_path / "server.pub.pem"
    pub_path.write_bytes(server_signer.public_key_pem)

    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    machine_id = uuid.uuid4()

    client = JwtClient(
        server_url="https://test.nexus",
        tenant_id=tenant_id,
        machine_id=machine_id,
        key_path=key_path,
        jwt_cache_path=jwt_cache,
        server_pubkey_path=pub_path,
        # Force file backend so tests don't touch the real OS keychain.
        cache=FileJwtCache(jwt_cache),
    )
    initial = server_signer.sign(
        DaemonClaims(tenant_id=tenant_id, principal_id=principal_id, machine_id=machine_id),
        ttl=timedelta(hours=1),
    )
    client.store_token(initial)
    return client, server_signer, tenant_id, principal_id, machine_id


@respx.mock
def test_refresh_invokes_server(client_setup: ClientSetup) -> None:
    client, signer, t, p, m = client_setup
    fresh = signer.sign(
        DaemonClaims(tenant_id=t, principal_id=p, machine_id=m),
        ttl=timedelta(hours=1),
    )
    respx.post("https://test.nexus/v1/daemon/refresh").mock(
        return_value=httpx.Response(200, json={"jwt": fresh})
    )
    new = client.refresh_now()
    assert new == fresh


@respx.mock
def test_refresh_401_raises(client_setup: ClientSetup) -> None:
    client, *_ = client_setup
    respx.post("https://test.nexus/v1/daemon/refresh").mock(
        return_value=httpx.Response(401, json={"detail": "machine_revoked"})
    )
    with pytest.raises(JwtClientError, match="revoked"):
        client.refresh_now()


@respx.mock
def test_cache_persisted(client_setup: ClientSetup) -> None:
    client, signer, t, p, m = client_setup
    fresh = signer.sign(
        DaemonClaims(tenant_id=t, principal_id=p, machine_id=m),
        ttl=timedelta(hours=1),
    )
    respx.post("https://test.nexus/v1/daemon/refresh").mock(
        return_value=httpx.Response(200, json={"jwt": fresh})
    )
    client.refresh_now()
    assert client.jwt_cache_path.read_text().strip() == fresh


def test_current_returns_cached(client_setup: ClientSetup) -> None:
    client, *_ = client_setup
    assert client.current() is not None


def test_current_valid_returns_token_when_far_from_expiry(client_setup: ClientSetup) -> None:
    """A freshly-issued 1-hour token must pass the 60s margin check."""
    client, *_ = client_setup
    assert client.current_valid(margin_s=60) is not None


def test_current_valid_returns_none_when_near_expiry(client_setup: ClientSetup) -> None:
    """A token with 10s remaining must be rejected by a 60s margin."""
    client, signer, t, p, m = client_setup
    near_exp = signer.sign(
        DaemonClaims(tenant_id=t, principal_id=p, machine_id=m),
        ttl=timedelta(seconds=10),
    )
    client.store_token(near_exp)
    assert client.current_valid(margin_s=60) is None
    # Plenty-of-margin call accepts the same token.
    assert client.current_valid(margin_s=1) is not None


def test_current_valid_returns_none_when_undecodable(client_setup: ClientSetup) -> None:
    """A malformed token must fail closed (force refresh at call site)."""
    client, *_ = client_setup
    client.store_token("not.a.jwt")
    assert client.current_valid(margin_s=60) is None


def test_seconds_until_expiry_tracks_ttl(client_setup: ClientSetup) -> None:
    """seconds_until_expiry must report a positive value for a fresh token."""
    client, signer, t, p, m = client_setup
    fresh = signer.sign(
        DaemonClaims(tenant_id=t, principal_id=p, machine_id=m),
        ttl=timedelta(hours=1),
    )
    client.store_token(fresh)
    remaining = client.seconds_until_expiry()
    assert remaining is not None
    # Should be close to 1 hour, well above 1 minute.
    assert 60.0 < remaining < 3700.0


@respx.mock
def test_refresh_rejects_token_signed_by_wrong_key(client_setup: ClientSetup) -> None:
    """A JWT signed by a DIFFERENT key must be rejected, cache must stay.

    Regression: previously ``refresh_now`` cached any 200-body ``jwt``
    string verbatim, so a misrouted / MITMed response could replace the
    daemon's trusted token. We pin the server pubkey at enroll-time and
    verify signature before caching.
    """
    client, _signer, t, p, m = client_setup
    # Take a note of the current cached token; it must NOT change on failure.
    prior = client.current()

    # Mint a JWT with a DIFFERENT signing key (simulates a malicious
    # response claiming to be the server).
    attacker_k = ec.generate_private_key(ec.SECP256R1())
    attacker_pem = attacker_k.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    attacker_signer = JwtSigner.from_pem(attacker_pem, issuer="https://test.nexus")
    forged = attacker_signer.sign(
        DaemonClaims(tenant_id=t, principal_id=p, machine_id=m),
        ttl=timedelta(hours=1),
    )
    respx.post("https://test.nexus/v1/daemon/refresh").mock(
        return_value=httpx.Response(200, json={"jwt": forged})
    )

    with pytest.raises(JwtClientError, match="signature|invalid"):
        client.refresh_now()
    assert client.current() == prior


@respx.mock
def test_refresh_rejects_token_for_wrong_machine(
    client_setup: ClientSetup,
) -> None:
    """Valid signature but mismatched machine_id claim must also reject."""
    client, signer, t, p, _m = client_setup
    prior = client.current()
    wrong_machine = uuid.uuid4()  # different from the one the client was built with
    forged_for_other_machine = signer.sign(
        DaemonClaims(tenant_id=t, principal_id=p, machine_id=wrong_machine),
        ttl=timedelta(hours=1),
    )
    respx.post("https://test.nexus/v1/daemon/refresh").mock(
        return_value=httpx.Response(200, json={"jwt": forged_for_other_machine})
    )
    with pytest.raises(JwtClientError, match="machine_id mismatch"):
        client.refresh_now()
    assert client.current() == prior


@respx.mock
def test_refresh_rejects_token_for_wrong_tenant(
    client_setup: ClientSetup,
) -> None:
    """A valid token for a DIFFERENT tenant must also reject."""
    client, signer, _t, p, m = client_setup
    prior = client.current()
    wrong_tenant = uuid.uuid4()
    forged_for_other_tenant = signer.sign(
        DaemonClaims(tenant_id=wrong_tenant, principal_id=p, machine_id=m),
        ttl=timedelta(hours=1),
    )
    respx.post("https://test.nexus/v1/daemon/refresh").mock(
        return_value=httpx.Response(200, json={"jwt": forged_for_other_tenant})
    )
    with pytest.raises(JwtClientError, match="tenant_id mismatch"):
        client.refresh_now()
    assert client.current() == prior
