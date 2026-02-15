"""Integration tests for Pyroscope continuous profiling (Issue #763).

Validates that infrastructure config files are correct and that
the profiling module integrates properly with existing observability
components (Grafana datasources, Docker Compose, Tempo correlation).
"""

from __future__ import annotations

from pathlib import Path

import yaml

OBSERVABILITY_DIR = Path(__file__).resolve().parents[2] / "observability"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_yaml(path: Path) -> dict:
    """Load and parse a YAML file, failing the test on syntax errors."""
    assert path.exists(), f"Config file missing: {path}"
    with open(path) as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict), f"Expected top-level dict in {path.name}"
    return data


# ---------------------------------------------------------------------------
# Pyroscope config validation
# ---------------------------------------------------------------------------


class TestPyroscopeConfig:
    """Validate observability/pyroscope/pyroscope.yml structure."""

    CONFIG = OBSERVABILITY_DIR / "pyroscope" / "pyroscope.yml"

    def test_file_is_valid_yaml(self) -> None:
        _load_yaml(self.CONFIG)

    def test_server_port_4040(self) -> None:
        data = _load_yaml(self.CONFIG)
        assert data["server"]["http_listen_port"] == 4040

    def test_storage_backend_filesystem(self) -> None:
        data = _load_yaml(self.CONFIG)
        assert data["storage"]["backend"] == "filesystem"

    def test_self_profiling_disabled(self) -> None:
        data = _load_yaml(self.CONFIG)
        assert data["self_profiling"]["disable_push"] is True

    def test_max_nodes_limit(self) -> None:
        data = _load_yaml(self.CONFIG)
        assert data["limits"]["max_nodes_per_profile"] == 16384


# ---------------------------------------------------------------------------
# Grafana datasource validation
# ---------------------------------------------------------------------------


class TestGrafanaDatasourceIntegration:
    """Validate Pyroscope entry in Grafana datasource provisioning."""

    DS_PATH = OBSERVABILITY_DIR / "grafana" / "provisioning" / "datasources" / "datasources.yml"

    def test_pyroscope_datasource_present(self) -> None:
        data = _load_yaml(self.DS_PATH)
        names = {ds["name"] for ds in data["datasources"]}
        assert "Pyroscope" in names

    def test_pyroscope_datasource_type(self) -> None:
        data = _load_yaml(self.DS_PATH)
        pyroscope_ds = next(ds for ds in data["datasources"] if ds["name"] == "Pyroscope")
        assert pyroscope_ds["type"] == "grafana-pyroscope-datasource"

    def test_pyroscope_datasource_uid(self) -> None:
        data = _load_yaml(self.DS_PATH)
        pyroscope_ds = next(ds for ds in data["datasources"] if ds["name"] == "Pyroscope")
        assert pyroscope_ds["uid"] == "pyroscope"

    def test_pyroscope_url(self) -> None:
        data = _load_yaml(self.DS_PATH)
        pyroscope_ds = next(ds for ds in data["datasources"] if ds["name"] == "Pyroscope")
        assert pyroscope_ds["url"] == "http://pyroscope:4040"


# ---------------------------------------------------------------------------
# Tempo trace-to-profile correlation
# ---------------------------------------------------------------------------


class TestTempoTraceToProfile:
    """Validate tracesToProfiles config on Tempo datasource."""

    DS_PATH = OBSERVABILITY_DIR / "grafana" / "provisioning" / "datasources" / "datasources.yml"

    def test_tempo_has_traces_to_profiles(self) -> None:
        data = _load_yaml(self.DS_PATH)
        tempo_ds = next(ds for ds in data["datasources"] if ds["name"] == "Tempo")
        assert "tracesToProfiles" in tempo_ds["jsonData"]

    def test_traces_to_profiles_points_to_pyroscope(self) -> None:
        data = _load_yaml(self.DS_PATH)
        tempo_ds = next(ds for ds in data["datasources"] if ds["name"] == "Tempo")
        t2p = tempo_ds["jsonData"]["tracesToProfiles"]
        assert t2p["datasourceUid"] == "pyroscope"

    def test_traces_to_profiles_has_profile_type(self) -> None:
        data = _load_yaml(self.DS_PATH)
        tempo_ds = next(ds for ds in data["datasources"] if ds["name"] == "Tempo")
        t2p = tempo_ds["jsonData"]["tracesToProfiles"]
        assert "profileTypeId" in t2p
        assert "cpu" in t2p["profileTypeId"]


# ---------------------------------------------------------------------------
# Docker Compose validation
# ---------------------------------------------------------------------------


class TestDockerComposeIntegration:
    """Validate pyroscope service in docker-compose.observability.yml."""

    COMPOSE_PATH = PROJECT_ROOT / "docker-compose.observability.yml"

    def test_compose_is_valid_yaml(self) -> None:
        _load_yaml(self.COMPOSE_PATH)

    def test_pyroscope_service_present(self) -> None:
        data = _load_yaml(self.COMPOSE_PATH)
        assert "pyroscope" in data["services"]

    def test_pyroscope_image(self) -> None:
        data = _load_yaml(self.COMPOSE_PATH)
        svc = data["services"]["pyroscope"]
        assert "grafana/pyroscope" in svc["image"]

    def test_pyroscope_port_4040(self) -> None:
        data = _load_yaml(self.COMPOSE_PATH)
        svc = data["services"]["pyroscope"]
        ports = [str(p) for p in svc["ports"]]
        assert any("4040" in p for p in ports)

    def test_pyroscope_has_observability_profile(self) -> None:
        data = _load_yaml(self.COMPOSE_PATH)
        svc = data["services"]["pyroscope"]
        assert "observability" in svc["profiles"]

    def test_pyroscope_volume_defined(self) -> None:
        data = _load_yaml(self.COMPOSE_PATH)
        assert "pyroscope-data" in data["volumes"]

    def test_nexus_server_has_pyroscope_env(self) -> None:
        data = _load_yaml(self.COMPOSE_PATH)
        env = data["services"]["nexus-server"]["environment"]
        assert "PYROSCOPE_ENABLED=true" in env
        assert "PYROSCOPE_SERVER_ADDRESS=http://pyroscope:4040" in env

    def test_grafana_depends_on_pyroscope(self) -> None:
        data = _load_yaml(self.COMPOSE_PATH)
        deps = data["services"]["grafana"]["depends_on"]
        assert "pyroscope" in deps
