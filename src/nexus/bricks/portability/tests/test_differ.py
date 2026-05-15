"""Tests for bundle diff."""

import json
import tarfile
from pathlib import Path

from nexus.bricks.portability.differ import (
    diff_bundles,
)


def _write_minimal_bundle(path: Path, *, file_hashes: list[str], merkle: str) -> None:
    """Create a minimal v2-shaped bundle with the given file checksums."""
    import shutil

    work = path.parent / (path.stem + "_work")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    files = {f"content/cas/{h[:2]}/{h}": h for h in file_hashes}
    manifest = {
        "format_version": "2.0.0",
        "nexus_version": "0.10.0",
        "bundle_id": "b",
        "source_instance": "hub",
        "source_zone_id": "eng",
        "export_timestamp": "2026-05-01T00:00:00+00:00",
        "file_count": len(file_hashes),
        "total_size_bytes": 0,
        "content_blob_count": len(file_hashes),
        "permission_count": 0,
        "include_content": True,
        "include_permissions": True,
        "include_embeddings": False,
        "checksums": {
            "algorithm": "sha256",
            "files": {
                p: {"path": p, "algorithm": "sha256", "hash": h, "size_bytes": 0}
                for p, h in files.items()
            },
            "merkle_root": merkle,
        },
        "archive_kind": "full",
        "embedding_model": "bge",
        "embedding_dim": 384,
        "placeholders": [],
        "min_nexus_version": "0.0.0",
    }
    (work / "manifest.json").write_text(json.dumps(manifest))
    (work / "metadata").mkdir()
    (work / "metadata" / "files.jsonl").write_text("")
    with tarfile.open(path, "w:gz") as tar:
        for f in sorted(work.rglob("*")):
            if f.is_file():
                tar.add(f, arcname=str(f.relative_to(work)))


def test_diff_no_changes(tmp_path):
    a = tmp_path / "a.nexus"
    b = tmp_path / "b.nexus"
    _write_minimal_bundle(a, file_hashes=["aaaa", "bbbb"], merkle="root1")
    _write_minimal_bundle(b, file_hashes=["aaaa", "bbbb"], merkle="root1")
    d = diff_bundles(a, b)
    assert d.added == set()
    assert d.removed == set()
    assert d.unchanged == {"aaaa", "bbbb"}


def test_diff_added_and_removed(tmp_path):
    a = tmp_path / "a.nexus"
    b = tmp_path / "b.nexus"
    _write_minimal_bundle(a, file_hashes=["aaaa", "bbbb"], merkle="r1")
    _write_minimal_bundle(b, file_hashes=["bbbb", "cccc"], merkle="r2")
    d = diff_bundles(a, b)
    assert d.removed == {"aaaa"}
    assert d.added == {"cccc"}
    assert d.unchanged == {"bbbb"}


def test_diff_summary_text(tmp_path):
    a = tmp_path / "a.nexus"
    b = tmp_path / "b.nexus"
    _write_minimal_bundle(a, file_hashes=["aaaa"], merkle="r1")
    _write_minimal_bundle(b, file_hashes=["bbbb"], merkle="r2")
    d = diff_bundles(a, b)
    text = d.summary()
    assert "+1 docs" in text
    assert "-1 docs" in text
