"""Structural tests for CONNECTOR_MANIFEST (Issue #3830 sub-project A.3)."""

from __future__ import annotations

from nexus.backends._manifest import CONNECTOR_MANIFEST, ConnectorManifestEntry
from nexus.backends.base.runtime_deps import (
    BinaryDep,
    PythonDep,
    ServiceDep,
)


class TestManifestStructure:
    def test_manifest_is_non_empty(self) -> None:
        assert len(CONNECTOR_MANIFEST) > 0

    def test_every_entry_has_required_fields(self) -> None:
        for entry in CONNECTOR_MANIFEST:
            assert isinstance(entry, ConnectorManifestEntry)
            assert entry.name, f"empty name in manifest entry: {entry!r}"
            assert entry.module_path, f"empty module_path: {entry.name}"
            assert entry.class_name, f"empty class_name: {entry.name}"
            assert entry.description, f"empty description: {entry.name}"
            assert entry.category, f"empty category: {entry.name}"

    def test_names_are_unique(self) -> None:
        names = [e.name for e in CONNECTOR_MANIFEST]
        dupes = {n for n in names if names.count(n) > 1}
        assert not dupes, f"duplicate manifest names: {sorted(dupes)}"

    def test_runtime_deps_are_typed(self) -> None:
        for entry in CONNECTOR_MANIFEST:
            for dep in entry.runtime_deps:
                assert isinstance(dep, (PythonDep, BinaryDep, ServiceDep)), (
                    f"{entry.name}: unexpected dep type {type(dep).__name__}"
                )

    def test_expected_connectors_present(self) -> None:
        """Python-registered A-full connectors must appear in the manifest.

        ``anthropic_native`` and ``openai_compatible`` migrated to the
        Rust LLM backend layer (develop commit 5461136d71b) — the names
        remain valid at the kernel dispatch layer but are no longer
        Python-registered connectors.
        """
        names = {e.name for e in CONNECTOR_MANIFEST}
        expected = {
            "path_gcs",
            "cas_gcs",
            "path_s3",
            "path_local",
            "cas_local",
            "local_connector",
            "gdrive_connector",
            "gmail_connector",
            "calendar_connector",
            "gcalendar_connector",
            "x_connector",
            "slack_connector",
            "hn_connector",
            "gws_gmail",
            "gws_calendar",
            "gws_sheets",
            "gws_docs",
            "gws_chat",
            "gws_drive",
            "github_connector",
            "gws_github",
        }
        missing = expected - names
        assert not missing, f"missing manifest entries: {sorted(missing)}"
        extra = names - expected
        assert not extra, f"unexpected manifest entries (not in inventory): {sorted(extra)}"

    def test_manifest_covers_every_builtin_register_call(self) -> None:
        """Every @register_connector first-positional-arg in src/nexus/backends/
        must appear as a CONNECTOR_MANIFEST entry name."""
        import ast
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[3]
        backends_root = repo_root / "src" / "nexus" / "backends"
        manifest_names = {e.name for e in CONNECTOR_MANIFEST}

        register_names: set[str] = set()
        for py_file in backends_root.rglob("*.py"):
            # Skip the manifest module and its own tests
            if py_file.name == "_manifest.py":
                continue
            try:
                tree = ast.parse(py_file.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    if (
                        isinstance(func, ast.Name)
                        and func.id == "register_connector"
                        and node.args
                        and isinstance(node.args[0], ast.Constant)
                    ):
                        register_names.add(node.args[0].value)

        missing_from_manifest = register_names - manifest_names
        assert not missing_from_manifest, (
            f"@register_connector sites lack a manifest entry: {sorted(missing_from_manifest)}"
        )
