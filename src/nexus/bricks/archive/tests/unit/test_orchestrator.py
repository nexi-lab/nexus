"""Tests for multi-zone archive orchestrator."""

from unittest.mock import MagicMock

from nexus.bricks.archive.orchestrator import ArchiveOrchestrator


def test_create_one_archive_per_zone(tmp_path):
    fake_export_service = MagicMock()
    fake_export_service.export_zone.side_effect = lambda zone_id, _options, **_kw: MagicMock(
        bundle_id=f"b-{zone_id}"
    )

    orch = ArchiveOrchestrator(export_service=fake_export_service, output_dir=tmp_path)
    manifests = orch.create_archives(zone_ids=["eng", "ops"], strip=True, sign=True)

    assert len(manifests) == 2
    assert fake_export_service.export_zone.call_count == 2
    paths = [
        c.kwargs.get("options").output_path if "options" in c.kwargs else c.args[1].output_path
        for c in fake_export_service.export_zone.call_args_list
    ]
    assert all(p.parent == tmp_path for p in paths)


def test_all_zones_uses_zone_lister(tmp_path):
    fake_export_service = MagicMock()
    fake_export_service.export_zone.return_value = MagicMock()

    orch = ArchiveOrchestrator(
        export_service=fake_export_service,
        output_dir=tmp_path,
        zone_lister=lambda: ["a", "b", "c"],
    )
    manifests = orch.create_archives(zone_ids=None, strip=True, sign=True)
    assert len(manifests) == 3


def test_strip_and_sign_options_propagate(tmp_path):
    fake_export_service = MagicMock()
    orch = ArchiveOrchestrator(export_service=fake_export_service, output_dir=tmp_path)
    orch.create_archives(zone_ids=["eng"], strip=False, sign=False)
    options = (
        fake_export_service.export_zone.call_args.kwargs.get("options")
        or fake_export_service.export_zone.call_args.args[1]
    )
    assert options.strip_credentials is False
    assert options.sign is False
