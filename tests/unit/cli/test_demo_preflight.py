"""Static checks for demo init remote preflight behavior."""

from __future__ import annotations

from pathlib import Path

DEMO_COMMAND = Path(__file__).resolve().parents[3] / "src/nexus/cli/commands/demo.py"


def test_remote_demo_preflight_avoids_root_metadata_probe() -> None:
    text = DEMO_COMMAND.read_text()
    remote_block = text[
        text.index('if preset in ("shared", "demo"):') : text.index("# Local preset")
    ]

    assert 'test_client.get("/api/v2/connectors")' in remote_block
    assert 'test_client.get("/api/v2/files/metadata", params={"path": "/"})' not in remote_block
