"""Static checks for demo init remote preflight behavior."""

from __future__ import annotations

from pathlib import Path

DEMO_COMMAND = Path(__file__).resolve().parents[3] / "src/nexus/cli/commands/demo.py"


def test_remote_demo_preflight_avoids_root_metadata_probe() -> None:
    text = DEMO_COMMAND.read_text()
    remote_block = text[
        text.index('if preset in ("shared", "demo"):') : text.index("# Local preset")
    ]

    assert 'test_client.get("/healthz/ready")' in remote_block
    assert 'test_client.get("/api/v2/connectors")' not in remote_block
    assert 'test_client.get("/api/v2/files/metadata", params={"path": "/"})' not in remote_block


def test_runtime_connection_prefers_nexus_url_env(monkeypatch, tmp_path) -> None:
    from nexus.cli.commands.demo import _resolve_runtime_connection

    monkeypatch.setenv("NEXUS_URL", "http://127.0.0.1:2026")

    runtime = _resolve_runtime_connection(
        {
            "preset": "demo",
            "data_dir": str(tmp_path),
            "ports": {"http": 2026, "grpc": 2028},
            "api_key": "sk-test",
        }
    )

    assert runtime["base_url"] == "http://127.0.0.1:2026"


def test_runtime_connection_prefers_server_url_config(monkeypatch, tmp_path) -> None:
    from nexus.cli.commands.demo import _resolve_runtime_connection

    monkeypatch.delenv("NEXUS_URL", raising=False)

    runtime = _resolve_runtime_connection(
        {
            "preset": "demo",
            "server_url": "http://127.0.0.1:2026",
            "data_dir": str(tmp_path),
            "ports": {"http": 2026, "grpc": 2028},
            "api_key": "sk-test",
        }
    )

    assert runtime["base_url"] == "http://127.0.0.1:2026"
