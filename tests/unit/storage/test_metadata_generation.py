from __future__ import annotations

import pytest

from nexus.contracts.exceptions import ValidationError
from nexus.contracts.metadata import FileMetadata
from nexus.storage._metadata_mapper_generated import MetadataMapper


def test_metadata_json_round_trip_preserves_generation() -> None:
    meta = FileMetadata(path="/docs/a.txt", size=5, content_id="cid", gen=7)

    encoded = MetadataMapper.to_json(meta)
    restored = MetadataMapper.from_json(encoded)

    assert encoded["gen"] == 7
    assert restored.gen == 7


def test_metadata_json_missing_generation_defaults_to_zero() -> None:
    restored = MetadataMapper.from_json({"path": "/docs/a.txt", "size": 5})

    assert restored.gen == 0


def test_metadata_proto_round_trip_preserves_generation() -> None:
    meta = FileMetadata(path="/docs/a.txt", size=5, content_id="cid", gen=11)

    proto = MetadataMapper.to_proto(meta)
    restored = MetadataMapper.from_proto(proto)

    assert proto.gen == 11
    assert restored.gen == 11


def test_metadata_compact_round_trip_preserves_generation() -> None:
    meta = FileMetadata(path="/docs/a.txt", size=5, content_id="cid", gen=13)

    compact = meta.to_compact()
    restored = FileMetadata.from_compact(compact)

    assert compact.gen == 13
    assert restored.gen == 13


def test_metadata_validate_rejects_negative_generation() -> None:
    meta = FileMetadata(path="/docs/a.txt", size=5, gen=-1)

    with pytest.raises(ValidationError, match="gen cannot be negative"):
        meta.validate()
