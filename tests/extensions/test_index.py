"""Index generator — deterministic JSON build + drift detection."""

from __future__ import annotations

import json

from nexus.extensions.index import build_index, verify_index
from nexus.extensions.store import INDEX_SCHEMA_VERSION


class TestBuildIndex:
    def test_build_serializes_manifests(self, tmp_path, all_manifests):
        out = tmp_path / "extensions.json"
        build_index(manifests=all_manifests, output_path=out)

        payload = json.loads(out.read_text())
        assert payload["schema_version"] == INDEX_SCHEMA_VERSION
        assert "generated_at" in payload
        names = {m["name"] for m in payload["manifests"]}
        assert names == {"hn", "search", "koi"}

    def test_build_is_deterministic(self, tmp_path, all_manifests):
        """Same input → same bytes (excluding generated_at)."""
        out1 = tmp_path / "a.json"
        out2 = tmp_path / "b.json"
        build_index(manifests=all_manifests, output_path=out1, frozen_time="X")
        build_index(
            manifests=list(reversed(all_manifests)),
            output_path=out2,
            frozen_time="X",
        )

        # Output is deterministic: sorted by (kind, name), stable formatting.
        assert out1.read_text() == out2.read_text()

    def test_index_round_trip_via_store(self, tmp_path, all_manifests):
        from nexus.extensions.store import ManifestStore

        out = tmp_path / "extensions.json"
        build_index(manifests=all_manifests, output_path=out)

        store = ManifestStore()
        store.load_json_index(out)
        assert {m.name for m in store.list()} == {"hn", "search", "koi"}


class TestVerifyIndex:
    def test_verify_passes_on_match(self, tmp_path, all_manifests):
        out = tmp_path / "extensions.json"
        build_index(manifests=all_manifests, output_path=out, frozen_time="X")
        # Re-run with same input → no drift.
        result = verify_index(manifests=all_manifests, expected_path=out, frozen_time="X")
        assert result.is_clean is True
        assert result.diff is None

    def test_verify_detects_drift(self, tmp_path, all_manifests):
        out = tmp_path / "extensions.json"
        build_index(manifests=all_manifests, output_path=out, frozen_time="X")
        # Drop one manifest → drift expected.
        without_hn = [m for m in all_manifests if m.name != "hn"]
        result = verify_index(manifests=without_hn, expected_path=out, frozen_time="X")
        assert result.is_clean is False
        assert result.diff is not None

    def test_verify_ignores_generated_at_clock_drift(self, tmp_path, all_manifests):
        """Verify pulls generated_at from disk so wall-clock differences don't trigger drift."""
        out = tmp_path / "extensions.json"
        build_index(manifests=all_manifests, output_path=out, frozen_time="2026-01-01T00:00:00Z")
        # Re-run with no frozen_time and a different wall clock.
        result = verify_index(manifests=all_manifests, expected_path=out)
        assert result.is_clean is True


class TestSetFieldDeterminism:
    """Regression: frozenset fields (e.g. ConnectorManifest.capabilities) must
    serialize in stable order across PYTHONHASHSEED values."""

    def test_capabilities_serialize_stably(self, tmp_path):
        from nexus.extensions.manifest import ConnectorManifest

        m = ConnectorManifest(
            name="multi",
            module="m",
            factory="F",
            service_name="svc",
            capabilities=frozenset({"alpha", "zeta", "mike", "bravo", "yankee"}),
        )

        out_a = tmp_path / "a.json"
        out_b = tmp_path / "b.json"
        build_index(manifests=[m], output_path=out_a, frozen_time="X")
        build_index(manifests=[m], output_path=out_b, frozen_time="X")
        assert out_a.read_text() == out_b.read_text()

        payload = json.loads(out_a.read_text())
        caps = payload["manifests"][0]["capabilities"]
        assert caps == sorted(caps), f"capabilities not sorted: {caps}"


class TestStrictDuplicateDetection:
    """Regression: two `_manifest.py` files with same (kind, name) must fail
    the build/verify hook instead of silently letting the first one win."""

    def test_duplicate_in_tree_manifests_raise(self, tmp_path, monkeypatch):
        from nexus.extensions import index as index_mod

        # Build a fake source tree: two connectors both naming "dup".
        connectors_root = tmp_path / "src" / "nexus" / "backends" / "connectors"
        for sub in ("a", "b"):
            child = connectors_root / sub
            child.mkdir(parents=True)
            (child / "_manifest.py").write_text(
                "from nexus.extensions.manifest import ConnectorManifest\n"
                "MANIFEST = ConnectorManifest(name='dup', module='m', factory='F',"
                " service_name='svc')\n"
            )

        fake_extensions_dir = tmp_path / "src" / "nexus" / "extensions"
        fake_extensions_dir.mkdir(parents=True)

        # Point _discover_in_tree_manifests at the fake tree by patching
        # the module's __file__ resolution.
        monkeypatch.setattr(
            index_mod,
            "__file__",
            str(fake_extensions_dir / "index.py"),
        )

        import pytest

        with pytest.raises(RuntimeError, match="duplicate in-tree manifests"):
            index_mod._discover_in_tree_manifests()
