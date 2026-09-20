from __future__ import annotations

from click.testing import CliRunner

from nexus.cli.commands import zone as zone_module


def test_create_uses_public_v2_api(monkeypatch) -> None:
    calls = []

    def fake_call(remote_url, remote_api_key, method, path, **kwargs):
        calls.append((remote_url, remote_api_key, method, path, kwargs))
        return {"operation_id": "op-1"}

    monkeypatch.setattr(zone_module, "api_call", fake_call)
    result = CliRunner().invoke(
        zone_module.zone,
        ["create", "team-alpha", "--remote-url", "http://localhost:2026"],
    )
    assert result.exit_code == 0, result.output
    assert calls[0][2:4] == ("POST", "/v2/zones")
    assert calls[0][4]["json_body"]["zone_id"] == "team-alpha"


def test_mount_and_unmount_use_operation_api(monkeypatch) -> None:
    calls = []

    def fake_call(remote_url, remote_api_key, method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == "GET":
            return {
                "mounts": [
                    {
                        "mount_id": "mount-1",
                        "parent_zone_id": "root",
                        "target_zone_id": "team-alpha",
                        "path": "/shared",
                    }
                ]
            }
        return {"operation_id": "op-1"}

    monkeypatch.setattr(zone_module, "api_call", fake_call)
    runner = CliRunner()
    mounted = runner.invoke(
        zone_module.zone,
        [
            "mount",
            "/shared",
            "team-alpha",
            "--parent-zone",
            "root",
            "--remote-url",
            "http://localhost:2026",
        ],
    )
    assert mounted.exit_code == 0, mounted.output
    unmounted = runner.invoke(
        zone_module.zone,
        [
            "unmount",
            "/shared",
            "--parent-zone",
            "root",
            "--remote-url",
            "http://localhost:2026",
        ],
    )
    assert unmounted.exit_code == 0, unmounted.output
    assert [(method, path) for method, path, _ in calls] == [
        ("POST", "/v2/zone-mounts"),
        ("GET", "/v2/zone-mounts?zone_id=root"),
        ("DELETE", "/v2/zone-mounts/mount-1"),
    ]
