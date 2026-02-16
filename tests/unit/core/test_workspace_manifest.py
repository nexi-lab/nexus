"""Tests for WorkspaceManifest dataclass.

Issue #1264: Extracted manifest format shared between WorkspaceManager and OverlayResolver.
Pattern follows: tests/unit/backends/test_chunked_storage.py (TestChunkInfo, TestChunkedReference)
"""

from __future__ import annotations

import json

import pytest

from nexus.core.workspace_manifest import ManifestEntry, WorkspaceManifest


class TestManifestEntry:
    """Tests for ManifestEntry dataclass."""

    def test_to_dict(self) -> None:
        entry = ManifestEntry(content_hash="abc123", size=1024, mime_type="text/plain")
        result = entry.to_dict()
        assert result == {"hash": "abc123", "size": 1024, "mime_type": "text/plain"}

    def test_to_dict_none_mime_type(self) -> None:
        entry = ManifestEntry(content_hash="abc123", size=512)
        result = entry.to_dict()
        assert result == {"hash": "abc123", "size": 512, "mime_type": None}

    def test_from_dict(self) -> None:
        data = {"hash": "def456", "size": 2048, "mime_type": "application/json"}
        entry = ManifestEntry.from_dict(data)
        assert entry.content_hash == "def456"
        assert entry.size == 2048
        assert entry.mime_type == "application/json"

    def test_from_dict_missing_mime_type(self) -> None:
        data = {"hash": "ghi789", "size": 100}
        entry = ManifestEntry.from_dict(data)
        assert entry.mime_type is None

    def test_round_trip(self) -> None:
        original = ManifestEntry(content_hash="abc", size=42, mime_type="text/plain")
        reconstructed = ManifestEntry.from_dict(original.to_dict())
        assert reconstructed.content_hash == original.content_hash
        assert reconstructed.size == original.size
        assert reconstructed.mime_type == original.mime_type


class TestWorkspaceManifest:
    """Tests for WorkspaceManifest dataclass."""

    def test_empty_manifest(self) -> None:
        manifest = WorkspaceManifest()
        assert manifest.file_count == 0
        assert manifest.total_size == 0
        assert manifest.paths() == set()
        assert manifest.get("anything") is None

    def test_get_existing_path(self) -> None:
        manifest = WorkspaceManifest(
            entries={"file.txt": ManifestEntry(content_hash="abc", size=100)}
        )
        entry = manifest.get("file.txt")
        assert entry is not None
        assert entry.content_hash == "abc"
        assert entry.size == 100

    def test_get_missing_path(self) -> None:
        manifest = WorkspaceManifest(
            entries={"file.txt": ManifestEntry(content_hash="abc", size=100)}
        )
        assert manifest.get("other.txt") is None

    def test_paths(self) -> None:
        manifest = WorkspaceManifest(
            entries={
                "a.txt": ManifestEntry(content_hash="h1", size=10),
                "b.txt": ManifestEntry(content_hash="h2", size=20),
                "dir/c.txt": ManifestEntry(content_hash="h3", size=30),
            }
        )
        assert manifest.paths() == {"a.txt", "b.txt", "dir/c.txt"}

    def test_file_count(self) -> None:
        manifest = WorkspaceManifest(
            entries={
                "a.txt": ManifestEntry(content_hash="h1", size=10),
                "b.txt": ManifestEntry(content_hash="h2", size=20),
            }
        )
        assert manifest.file_count == 2

    def test_total_size(self) -> None:
        manifest = WorkspaceManifest(
            entries={
                "a.txt": ManifestEntry(content_hash="h1", size=100),
                "b.txt": ManifestEntry(content_hash="h2", size=200),
                "c.txt": ManifestEntry(content_hash="h3", size=300),
            }
        )
        assert manifest.total_size == 600


class TestWorkspaceManifestSerialization:
    """Tests for JSON serialization/deserialization."""

    def test_to_json_produces_valid_json(self) -> None:
        manifest = WorkspaceManifest(
            entries={
                "file.txt": ManifestEntry(content_hash="abc", size=100, mime_type="text/plain")
            }
        )
        data = manifest.to_json()
        parsed = json.loads(data)
        assert "file.txt" in parsed
        assert parsed["file.txt"]["hash"] == "abc"

    def test_round_trip(self) -> None:
        original = WorkspaceManifest(
            entries={
                "a.txt": ManifestEntry(content_hash="h1", size=100, mime_type="text/plain"),
                "dir/b.py": ManifestEntry(content_hash="h2", size=200, mime_type="text/x-python"),
                "c.json": ManifestEntry(content_hash="h3", size=300),
            }
        )
        json_bytes = original.to_json()
        reconstructed = WorkspaceManifest.from_json(json_bytes)

        assert reconstructed.file_count == original.file_count
        assert reconstructed.total_size == original.total_size
        assert reconstructed.paths() == original.paths()

        for path in original.paths():
            orig_entry = original.get(path)
            recon_entry = reconstructed.get(path)
            assert recon_entry is not None
            assert orig_entry is not None
            assert recon_entry.content_hash == orig_entry.content_hash
            assert recon_entry.size == orig_entry.size
            assert recon_entry.mime_type == orig_entry.mime_type

    def test_empty_manifest_round_trip(self) -> None:
        original = WorkspaceManifest()
        json_bytes = original.to_json()
        reconstructed = WorkspaceManifest.from_json(json_bytes)
        assert reconstructed.file_count == 0
        assert reconstructed.entries == {}

    def test_to_json_sorted_by_path(self) -> None:
        """Entries must be sorted by path for deterministic hashing."""
        manifest = WorkspaceManifest(
            entries={
                "z.txt": ManifestEntry(content_hash="h1", size=10),
                "a.txt": ManifestEntry(content_hash="h2", size=20),
                "m.txt": ManifestEntry(content_hash="h3", size=30),
            }
        )
        json_bytes = manifest.to_json()
        json_str = json_bytes.decode("utf-8")

        # Verify order: a.txt comes before m.txt comes before z.txt
        a_pos = json_str.index("a.txt")
        m_pos = json_str.index("m.txt")
        z_pos = json_str.index("z.txt")
        assert a_pos < m_pos < z_pos

    def test_deterministic_hashing(self) -> None:
        """Same entries in different insertion order produce same JSON."""
        entries1 = {
            "b.txt": ManifestEntry(content_hash="h2", size=20),
            "a.txt": ManifestEntry(content_hash="h1", size=10),
        }
        entries2 = {
            "a.txt": ManifestEntry(content_hash="h1", size=10),
            "b.txt": ManifestEntry(content_hash="h2", size=20),
        }
        m1 = WorkspaceManifest(entries=entries1)
        m2 = WorkspaceManifest(entries=entries2)
        assert m1.to_json() == m2.to_json()

    def test_backward_compat_with_existing_snapshot_format(self) -> None:
        """Must parse JSON produced by the existing WorkspaceManager.create_snapshot()."""
        # This is the exact format produced by workspace_manager.py lines 239-257
        existing_json = b'{\n  "config.yaml": {"hash": "abc123", "size": 256, "mime_type": "application/yaml"},\n  "src/main.py": {"hash": "def456", "size": 1024, "mime_type": "text/x-python"}\n}'
        manifest = WorkspaceManifest.from_json(existing_json)

        assert manifest.file_count == 2
        config_entry = manifest.get("config.yaml")
        assert config_entry is not None
        assert config_entry.content_hash == "abc123"
        assert config_entry.size == 256
        assert config_entry.mime_type == "application/yaml"

        src_entry = manifest.get("src/main.py")
        assert src_entry is not None
        assert src_entry.content_hash == "def456"

    def test_backward_compat_null_mime_type(self) -> None:
        """Existing snapshots may have null mime_type values."""
        existing_json = b'{"file.bin": {"hash": "xyz", "size": 512, "mime_type": null}}'
        manifest = WorkspaceManifest.from_json(existing_json)
        entry = manifest.get("file.bin")
        assert entry is not None
        assert entry.mime_type is None

    def test_large_manifest(self) -> None:
        """Verify performance with 10K entries."""
        entries = {
            f"dir{i // 100}/file{i}.txt": ManifestEntry(
                content_hash=f"hash_{i:06d}",
                size=i * 100,
                mime_type="text/plain",
            )
            for i in range(10_000)
        }
        manifest = WorkspaceManifest(entries=entries)

        assert manifest.file_count == 10_000

        # Round-trip should work
        json_bytes = manifest.to_json()
        reconstructed = WorkspaceManifest.from_json(json_bytes)
        assert reconstructed.file_count == 10_000

    def test_from_json_invalid_json_raises(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            WorkspaceManifest.from_json(b"not json")

    def test_from_json_missing_hash_raises(self) -> None:
        with pytest.raises(KeyError):
            WorkspaceManifest.from_json(b'{"file.txt": {"size": 100}}')


class TestWorkspaceManifestFromFileList:
    """Tests for from_file_list constructor."""

    def test_from_file_list(self) -> None:
        file_entries = [
            ("a.txt", "h1", 100, "text/plain"),
            ("b.py", "h2", 200, "text/x-python"),
            ("c.bin", "h3", 300, None),
        ]
        manifest = WorkspaceManifest.from_file_list(file_entries)
        assert manifest.file_count == 3
        assert manifest.total_size == 600

        a_entry = manifest.get("a.txt")
        assert a_entry is not None
        assert a_entry.content_hash == "h1"
        assert a_entry.mime_type == "text/plain"

        c_entry = manifest.get("c.bin")
        assert c_entry is not None
        assert c_entry.mime_type is None

    def test_from_empty_file_list(self) -> None:
        manifest = WorkspaceManifest.from_file_list([])
        assert manifest.file_count == 0

    def test_special_characters_in_path(self) -> None:
        """Paths with special characters should serialize/deserialize correctly."""
        file_entries = [
            ("path with spaces/file.txt", "h1", 100, None),
            ('path"with"quotes.txt', "h2", 200, None),
        ]
        manifest = WorkspaceManifest.from_file_list(file_entries)
        json_bytes = manifest.to_json()
        reconstructed = WorkspaceManifest.from_json(json_bytes)

        assert reconstructed.get("path with spaces/file.txt") is not None
        assert reconstructed.get('path"with"quotes.txt') is not None
