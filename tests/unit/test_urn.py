"""Tests for NexusURN — path-based entity locators (Issue #2929)."""

import hashlib

import pytest

from nexus.contracts.urn import NexusURN


class TestNexusURN:
    """NexusURN value type tests."""

    def test_create_file_urn_hashes_path(self) -> None:
        """for_file() hashes the path to produce the identifier (locator semantics)."""
        urn = NexusURN.for_file("zone_acme", "/data/file.csv")
        expected_hash = hashlib.sha256(b"/data/file.csv").hexdigest()[:32]
        assert urn.entity_type == "file"
        assert urn.zone_id == "zone_acme"
        assert urn.identifier == expected_hash

    def test_str_format(self) -> None:
        urn = NexusURN(entity_type="file", zone_id="zone1", identifier="abc123")
        assert str(urn) == "urn:nexus:file:zone1:abc123"

    def test_parse_valid_urn(self) -> None:
        urn = NexusURN.parse("urn:nexus:file:zone_acme:abc123def456")
        assert urn.entity_type == "file"
        assert urn.zone_id == "zone_acme"
        assert urn.identifier == "abc123def456"

    def test_parse_invalid_urn_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid Nexus URN format"):
            NexusURN.parse("not-a-urn")

    def test_parse_missing_prefix_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid Nexus URN format"):
            NexusURN.parse("urn:other:file:zone:id")

    def test_roundtrip(self) -> None:
        original = NexusURN(entity_type="schema", zone_id="z1", identifier="id42abcdef")
        parsed = NexusURN.parse(str(original))
        assert parsed == original

    def test_frozen_immutable(self) -> None:
        urn = NexusURN.for_file("zone1", "/some/path")
        with pytest.raises(AttributeError):
            urn.entity_type = "directory"

    def test_for_directory(self) -> None:
        urn = NexusURN.for_directory("zone1", "/some/dir")
        assert urn.entity_type == "directory"
        assert urn.is_directory()
        assert not urn.is_file()

    def test_for_file_is_file(self) -> None:
        urn = NexusURN.for_file("zone1", "/some/file")
        assert urn.is_file()
        assert not urn.is_directory()

    def test_empty_entity_type_raises(self) -> None:
        with pytest.raises(ValueError, match="entity_type is required"):
            NexusURN(entity_type="", zone_id="zone1", identifier="id")

    def test_empty_zone_id_raises(self) -> None:
        with pytest.raises(ValueError, match="zone_id is required"):
            NexusURN(entity_type="file", zone_id="", identifier="id")

    def test_empty_identifier_raises(self) -> None:
        with pytest.raises(ValueError, match="identifier is required"):
            NexusURN(entity_type="file", zone_id="zone1", identifier="")

    def test_equality(self) -> None:
        """Same path → same URN (deterministic locator)."""
        urn1 = NexusURN.for_file("z", "/data/file.csv")
        urn2 = NexusURN.for_file("z", "/data/file.csv")
        assert urn1 == urn2

    def test_inequality(self) -> None:
        """Different path → different URN (locator changes on rename)."""
        urn1 = NexusURN.for_file("z", "/data/old.csv")
        urn2 = NexusURN.for_file("z", "/data/new.csv")
        assert urn1 != urn2

    def test_hashable(self) -> None:
        urn = NexusURN.for_file("z", "/data/file.csv")
        s = {urn}
        assert urn in s

    def test_parse_preserves_underscores(self) -> None:
        urn = NexusURN.parse("urn:nexus:file:zone_test:path_id_123")
        assert urn.zone_id == "zone_test"
        assert urn.identifier == "path_id_123"

    def test_build_urn_changes_on_rename(self) -> None:
        """URNs are locators: different path → different URN."""
        urn_old = NexusURN.for_file("z1", "/data/old.csv")
        urn_new = NexusURN.for_file("z1", "/data/new.csv")
        assert urn_old != urn_new

    def test_build_urn_same_path_same_result(self) -> None:
        """Same path always produces the same URN (deterministic)."""
        urn1 = NexusURN.for_file("z1", "/data/file.csv")
        urn2 = NexusURN.for_file("z1", "/data/file.csv")
        assert urn1 == urn2

    def test_from_metadata(self) -> None:
        """from_metadata computes URN from FileMetadata-like object."""

        class FakeMeta:
            path = "/data/file.csv"
            zone_id = "z1"

        urn = NexusURN.from_metadata(FakeMeta())
        expected = NexusURN.for_file("z1", "/data/file.csv")
        assert urn == expected
