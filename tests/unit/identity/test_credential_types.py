"""Unit tests for credential_types.py — pure value objects, no DB.

Tests the Ability enum, Capability dataclass (construction, validation,
serialization, subset checking), CredentialClaims, and CredentialStatus.
"""

import types
from datetime import datetime

import pytest

from nexus.contracts.credential_types import (
    MAX_BACKEND_FEATURES_PER_CREDENTIAL,
    MAX_DELEGATION_DEPTH,
    Ability,
    Capability,
    CredentialClaims,
    CredentialStatus,
)


class TestAbilityEnum:
    def test_all_values(self) -> None:
        assert Ability.READ == "read"
        assert Ability.WRITE == "write"
        assert Ability.EXECUTE == "execute"
        assert Ability.DELEGATE == "delegate"
        assert Ability.ADMIN == "*"

    def test_from_string(self) -> None:
        assert Ability("read") is Ability.READ
        assert Ability("*") is Ability.ADMIN

    def test_invalid_value(self) -> None:
        with pytest.raises(ValueError):
            Ability("invalid")


class TestCapability:
    def test_basic_construction(self) -> None:
        cap = Capability(resource="nexus:brick:search", abilities=(Ability.READ,))
        assert cap.resource == "nexus:brick:search"
        assert cap.abilities == (Ability.READ,)
        assert len(cap.caveats) == 0

    def test_with_caveats(self) -> None:
        caveats = types.MappingProxyType({"max_results": 100})
        cap = Capability(
            resource="nexus:brick:search",
            abilities=(Ability.READ, Ability.EXECUTE),
            caveats=caveats,
        )
        assert cap.caveats["max_results"] == 100

    def test_empty_resource_raises(self) -> None:
        with pytest.raises(ValueError, match="resource must be non-empty"):
            Capability(resource="", abilities=(Ability.READ,))

    def test_empty_abilities_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one ability"):
            Capability(resource="nexus:brick:search", abilities=())

    def test_to_dict(self) -> None:
        cap = Capability(
            resource="nexus:brick:cache",
            abilities=(Ability.READ, Ability.WRITE),
            caveats=types.MappingProxyType({"zone_id": "acme"}),
        )
        d = cap.to_dict()
        assert d["resource"] == "nexus:brick:cache"
        assert d["abilities"] == ["read", "write"]
        assert d["caveats"] == {"zone_id": "acme"}

    def test_to_dict_no_caveats(self) -> None:
        cap = Capability(resource="test", abilities=(Ability.READ,))
        d = cap.to_dict()
        assert "caveats" not in d

    def test_from_dict(self) -> None:
        data = {"resource": "nexus:brick:search", "abilities": ["read", "execute"]}
        cap = Capability.from_dict(data)
        assert cap.resource == "nexus:brick:search"
        assert cap.abilities == (Ability.READ, Ability.EXECUTE)

    def test_from_dict_with_caveats(self) -> None:
        data = {
            "resource": "test",
            "abilities": ["write"],
            "caveats": {"limit": 50},
        }
        cap = Capability.from_dict(data)
        assert cap.caveats["limit"] == 50

    def test_round_trip(self) -> None:
        original = Capability(
            resource="nexus:zone:acme",
            abilities=(Ability.READ, Ability.WRITE, Ability.DELEGATE),
            caveats=types.MappingProxyType({"max_depth": 2}),
        )
        reconstructed = Capability.from_dict(original.to_dict())
        assert reconstructed.resource == original.resource
        assert reconstructed.abilities == original.abilities
        assert dict(reconstructed.caveats) == dict(original.caveats)


class TestCapabilitySubset:
    def test_exact_match(self) -> None:
        parent = Capability(resource="nexus:brick:cache", abilities=(Ability.READ, Ability.WRITE))
        child = Capability(resource="nexus:brick:cache", abilities=(Ability.READ,))
        assert child.is_subset_of(parent)

    def test_admin_grants_all(self) -> None:
        parent = Capability(resource="nexus:brick:cache", abilities=(Ability.ADMIN,))
        child = Capability(resource="nexus:brick:cache", abilities=(Ability.READ, Ability.WRITE))
        assert child.is_subset_of(parent)

    def test_wildcard_resource(self) -> None:
        parent = Capability(resource="*", abilities=(Ability.READ,))
        child = Capability(resource="nexus:brick:search", abilities=(Ability.READ,))
        assert child.is_subset_of(parent)

    def test_different_resource_fails(self) -> None:
        parent = Capability(resource="nexus:brick:cache", abilities=(Ability.READ,))
        child = Capability(resource="nexus:brick:search", abilities=(Ability.READ,))
        assert not child.is_subset_of(parent)

    def test_additional_ability_fails(self) -> None:
        parent = Capability(resource="nexus:brick:cache", abilities=(Ability.READ,))
        child = Capability(resource="nexus:brick:cache", abilities=(Ability.READ, Ability.WRITE))
        assert not child.is_subset_of(parent)

    def test_wildcard_resource_with_admin(self) -> None:
        parent = Capability(resource="*", abilities=(Ability.ADMIN,))
        child = Capability(
            resource="anything", abilities=(Ability.READ, Ability.WRITE, Ability.EXECUTE)
        )
        assert child.is_subset_of(parent)


class TestCredentialClaims:
    def test_construction(self) -> None:
        claims = CredentialClaims(
            issuer_did="did:key:zIssuer",
            subject_did="did:key:zSubject",
            credential_id="urn:uuid:test",
            capabilities=(Capability(resource="test", abilities=(Ability.READ,)),),
            issued_at=1000.0,
            expires_at=2000.0,
        )
        assert claims.issuer_did == "did:key:zIssuer"
        assert claims.delegation_depth == 0
        assert claims.parent_credential_id is None

    def test_delegation_fields(self) -> None:
        claims = CredentialClaims(
            issuer_did="did:key:zIssuer",
            subject_did="did:key:zSubject",
            credential_id="urn:uuid:test",
            capabilities=(),
            issued_at=1000.0,
            expires_at=2000.0,
            parent_credential_id="urn:uuid:parent",
            delegation_depth=2,
        )
        assert claims.parent_credential_id == "urn:uuid:parent"
        assert claims.delegation_depth == 2


class TestCredentialStatus:
    def test_construction(self) -> None:
        now = datetime.now(tz=None)
        status = CredentialStatus(
            credential_id="urn:uuid:test",
            issuer_did="did:key:zIssuer",
            subject_did="did:key:zSubject",
            subject_agent_id="agent-1",
            is_active=True,
            created_at=now,
            expires_at=now,
        )
        assert status.is_active
        assert status.revoked_at is None
        assert status.delegation_depth == 0


class TestConstants:
    def test_max_capabilities(self) -> None:
        assert MAX_BACKEND_FEATURES_PER_CREDENTIAL == 20

    def test_max_delegation_depth(self) -> None:
        assert MAX_DELEGATION_DEPTH == 3
