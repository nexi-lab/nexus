"""CLI/SDK parity for the remote-connect contract (Issue #4132).

``nexus env --json`` must emit exactly the connection values the remote
SDK consumes: NEXUS_URL, NEXUS_API_KEY, NEXUS_GRPC_HOST, NEXUS_GRPC_PORT.
The remote SDK path needs gRPC, not just the HTTP URL.

Fixture approach mirrors tests/unit/cli/test_env_cmd.py: write a minimal
nexus.yaml + {data_dir}/.state.json into tmp_path, patch
nexus.cli.state.CONFIG_SEARCH_PATHS to point at it — no Docker, no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from nexus.cli.commands.env_cmd import env_cmd


@pytest.fixture()
def project_dir(tmp_path: Path) -> Path:
    """Minimal nexus project fixture: nexus.yaml + data_dir/.state.json."""
    data_dir = tmp_path / "nexus-data"
    data_dir.mkdir()

    config = {
        "preset": "shared",
        "data_dir": str(data_dir),
        "services": ["nexus", "postgres"],
        "ports": {"http": 2026, "grpc": 2028, "postgres": 5432},
        "api_key": "sk-test-key",
    }
    (tmp_path / "nexus.yaml").write_text(yaml.dump(config))

    state = {
        "version": 1,
        "ports": {"http": 3026, "grpc": 3028, "postgres": 5433},
        "api_key": "sk-runtime-key",
        "build_mode": "local",
        "image_used": "nexus:local-abc12345",
    }
    (data_dir / ".state.json").write_text(json.dumps(state))

    return tmp_path


def test_env_json_emits_grpc_and_http(project_dir: Path) -> None:
    """nexus env --json must contain all keys the remote SDK needs.

    Asserts:
    - NEXUS_URL        — HTTP base URL (remote SDK REST calls)
    - NEXUS_API_KEY    — bearer token for auth
    - NEXUS_GRPC_HOST  — gRPC host:port string (remote SDK streaming)
    - NEXUS_GRPC_PORT  — gRPC port as string, truthy

    Also validates that runtime state.json values win over nexus.yaml
    defaults (port 3026/3028 beats 2026/2028).
    """
    runner = CliRunner()
    with patch(
        "nexus.cli.state.CONFIG_SEARCH_PATHS",
        (str(project_dir / "nexus.yaml"),),
    ):
        result = runner.invoke(env_cmd, ["--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    # All four keys required by the remote SDK must be present.
    for key in ("NEXUS_URL", "NEXUS_API_KEY", "NEXUS_GRPC_HOST", "NEXUS_GRPC_PORT"):
        assert key in payload, f"{key} missing from `nexus env --json`; got: {list(payload)}"

    # HTTP URL must use the runtime port (state.json wins).
    assert payload["NEXUS_URL"] == "http://localhost:3026"

    # API key must reflect runtime state.json value.
    assert payload["NEXUS_API_KEY"] == "sk-runtime-key"

    # gRPC port must be truthy (non-empty, non-zero string).
    assert payload["NEXUS_GRPC_PORT"], "NEXUS_GRPC_PORT must be a truthy value"

    # gRPC port string must match the runtime port from state.json.
    assert payload["NEXUS_GRPC_PORT"] == "3028"

    # NEXUS_GRPC_HOST must reference the same gRPC port.
    assert "3028" in payload["NEXUS_GRPC_HOST"]
