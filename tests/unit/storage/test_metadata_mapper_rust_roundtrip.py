"""Cross-boundary round-trip tests for FileMetadata proto encoding.

R16.1a guard: ``FileMetadata.target_zone_id`` is the only field that
lets Rust distinguish a DT_MOUNT entry's parent zone from its target.
Before R16.1a the Rust ``KernelFileMetadata`` struct dropped the
field on proto decode — DT_MOUNT entries Python wrote via raft would
come back from Rust-side reads with ``target_zone_id = None``, silently
breaking cross-zone path resolution.

These tests run the Python → proto → Rust leg (via the
``nexus_kernel.file_metadata_from_proto_bytes`` debug helper) and the
reverse leg to assert the byte format is stable across the language
boundary for every field Rust tracks today.

Skipped when the Rust extension is not available (pure-Python CI).
"""

from __future__ import annotations

import unittest

from nexus.contracts.metadata import DT_DIR, DT_MOUNT, DT_REG, FileMetadata

try:
    from nexus_kernel import (
        file_metadata_from_proto_bytes,
        file_metadata_to_proto_bytes,
    )

    _HAS_RUST = True
except ImportError:
    _HAS_RUST = False


@unittest.skipUnless(_HAS_RUST, "nexus_kernel extension not built")
class MetadataMapperRustRoundtripTests(unittest.TestCase):
    """Assert MetadataMapper.to_proto bytes round-trip through Rust intact."""

    def _to_proto_bytes(self, meta: FileMetadata) -> bytes:
        """Encode via the Python MetadataMapper (SSOT for Python-side bytes)."""
        from nexus.storage._metadata_mapper_generated import MetadataMapper

        return MetadataMapper.to_proto(meta).SerializeToString()

    def test_dt_mount_target_zone_id_survives_rust_decode(self) -> None:
        """The primary R16.1a guard — DT_MOUNT target_zone_id round-trips."""
        meta = FileMetadata(
            path="/mnt/peer",
            backend_name="federation",
            physical_path="",
            size=0,
            version=1,
            entry_type=DT_MOUNT,
            zone_id="zone-a",
            target_zone_id="zone-b",
        )
        rust_view = file_metadata_from_proto_bytes(self._to_proto_bytes(meta))
        self.assertEqual(rust_view["target_zone_id"], "zone-b")
        self.assertEqual(rust_view["zone_id"], "zone-a")
        self.assertEqual(rust_view["entry_type"], DT_MOUNT)
        self.assertEqual(rust_view["path"], "/mnt/peer")

    def test_dt_reg_has_none_target_zone_id(self) -> None:
        """Non-mount entries decode to target_zone_id = None (proto3 default)."""
        meta = FileMetadata(
            path="/docs/readme.md",
            backend_name="local",
            physical_path="abc123",
            size=1024,
            version=1,
            entry_type=DT_REG,
            zone_id="zone-a",
        )
        rust_view = file_metadata_from_proto_bytes(self._to_proto_bytes(meta))
        self.assertIsNone(rust_view["target_zone_id"])
        self.assertEqual(rust_view["entry_type"], DT_REG)

    def test_rust_encode_python_decode_preserves_target_zone_id(self) -> None:
        """Reverse direction: Rust-encoded bytes decode via MetadataMapper."""
        from nexus.core import metadata_pb2
        from nexus.storage._metadata_mapper_generated import MetadataMapper

        rust_bytes = file_metadata_to_proto_bytes(
            "/mnt/other",
            "federation",
            "",
            0,
            1,
            DT_MOUNT,
            zone_id="zone-a",
            target_zone_id="zone-c",
        )
        proto = metadata_pb2.FileMetadata()
        proto.ParseFromString(bytes(rust_bytes))
        decoded = MetadataMapper.from_proto(proto)
        self.assertEqual(decoded.target_zone_id, "zone-c")
        self.assertEqual(decoded.zone_id, "zone-a")
        self.assertEqual(decoded.entry_type, DT_MOUNT)

    def test_empty_target_zone_id_encodes_as_proto_default(self) -> None:
        """Rust writes target_zone_id = None as the proto3 empty-string default."""
        rust_bytes = file_metadata_to_proto_bytes(
            "/a.txt",
            "local",
            "hash",
            100,
            1,
            DT_REG,
        )
        # Round-trip through Rust decode — should come back as None.
        rust_view = file_metadata_from_proto_bytes(bytes(rust_bytes))
        self.assertIsNone(rust_view["target_zone_id"])

    def test_dt_dir_fields_unaffected(self) -> None:
        """Regression guard: adding target_zone_id did not disturb other fields."""
        meta = FileMetadata(
            path="/docs",
            backend_name="local",
            physical_path="",
            size=0,
            version=1,
            entry_type=DT_DIR,
            zone_id="zone-a",
            mime_type="inode/directory",
        )
        rust_view = file_metadata_from_proto_bytes(self._to_proto_bytes(meta))
        self.assertEqual(rust_view["path"], "/docs")
        self.assertEqual(rust_view["entry_type"], DT_DIR)
        self.assertEqual(rust_view["mime_type"], "inode/directory")
        self.assertIsNone(rust_view["target_zone_id"])


if __name__ == "__main__":
    unittest.main()
