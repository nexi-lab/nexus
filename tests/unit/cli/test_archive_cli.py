"""Tests for nexus archive CLI."""

from click.testing import CliRunner

from nexus.cli.commands.archive import archive


def test_help_lists_subcommands():
    runner = CliRunner()
    result = runner.invoke(archive, ["--help"])
    assert result.exit_code == 0
    for sub in ["create", "verify", "restore", "diff", "inspect", "keys"]:
        assert sub in result.output


def test_inspect_shows_manifest_summary(tmp_path, monkeypatch):
    # Build a minimal v2 bundle
    import json
    import tarfile

    bundle_dir = tmp_path / "b"
    bundle_dir.mkdir()
    manifest = {
        "format_version": "2.0.0",
        "nexus_version": "0.10.0",
        "bundle_id": "b-1",
        "source_instance": "hub",
        "source_zone_id": "eng",
        "export_timestamp": "2026-05-01T00:00:00+00:00",
        "file_count": 0,
        "total_size_bytes": 0,
        "content_blob_count": 0,
        "permission_count": 0,
        "include_content": True,
        "include_permissions": True,
        "include_embeddings": False,
        "checksums": {"algorithm": "sha256", "files": {}, "merkle_root": None},
        "archive_kind": "full",
        "embedding_model": "bge",
        "embedding_dim": 384,
        "placeholders": [],
        "min_nexus_version": "0.0.0",
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest))
    out = tmp_path / "b.nexus"
    with tarfile.open(out, "w:gz") as tar:
        tar.add(bundle_dir / "manifest.json", arcname="manifest.json")

    runner = CliRunner()
    result = runner.invoke(archive, ["inspect", str(out)])
    assert result.exit_code == 0
    assert "eng" in result.output
    assert "2.0.0" in result.output
