"""Tests for NexusURN — stable entity identifiers (Issue #2929)."""

import pytest

from nexus.contracts.urn import NexusURN


class TestNexusURN:
    """NexusURN value type tests."""

    def test_create_file_urn(self) -> None:
        urn = NexusURN.for_file("zone_acme", "550e8400-e29b-41d4-a716-446655440000")
        assert urn.entity_type == "file"
        assert urn.zone_id == "zone_acme"
        assert urn.identifier == "550e8400-e29b-41d4-a716-446655440000"

    def test_str_format(self) -> None:
        urn = NexusURN(entity_type="file", zone_id="zone1", identifier="abc123")
        assert str(urn) == "urn:nexus:file:zone1:abc123"

    def test_parse_valid_urn(self) -> None:
        urn = NexusURN.parse("urn:nexus:file:zone_acme:abc-123")
        assert urn.entity_type == "file"
        assert urn.zone_id == "zone_acme"
        assert urn.identifier == "abc-123"

    def test_parse_invalid_urn_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid Nexus URN format"):
            NexusURN.parse("not-a-urn")

    def test_parse_missing_prefix_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid Nexus URN format"):
            NexusURN.parse("urn:other:file:zone:id")

    def test_roundtrip(self) -> None:
        original = NexusURN(entity_type="schema", zone_id="z1", identifier="id-42")
        parsed = NexusURN.parse(str(original))
        assert parsed == original

    def test_frozen_immutable(self) -> None:
        urn = NexusURN.for_file("zone1", "id1")
        with pytest.raises(AttributeError):
            urn.entity_type = "directory"

    def test_for_directory(self) -> None:
        urn = NexusURN.for_directory("zone1", "dir-id")
        assert urn.entity_type == "directory"
        assert urn.is_directory()
        assert not urn.is_file()

    def test_for_file_is_file(self) -> None:
        urn = NexusURN.for_file("zone1", "file-id")
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
        urn1 = NexusURN.for_file("z", "id1")
        urn2 = NexusURN.for_file("z", "id1")
        assert urn1 == urn2

    def test_inequality(self) -> None:
        urn1 = NexusURN.for_file("z", "id1")
        urn2 = NexusURN.for_file("z", "id2")
        assert urn1 != urn2

    def test_hashable(self) -> None:
        urn = NexusURN.for_file("z", "id1")
        s = {urn}
        assert urn in s

    def test_parse_preserves_underscores(self) -> None:
        urn = NexusURN.parse("urn:nexus:file:zone_test:path_id_123")
        assert urn.zone_id == "zone_test"
        assert urn.identifier == "path_id_123"
