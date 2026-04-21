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
        """All 22 A-full connectors must appear in the manifest."""
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
            "anthropic_native",
            "openai_compatible",
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
