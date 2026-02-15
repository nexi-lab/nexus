"""Tests for observability YAML config validation (Issue #761).

Validates that all infrastructure configuration files under observability/
are syntactically correct YAML and contain the required top-level keys.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

OBSERVABILITY_DIR = Path(__file__).resolve().parents[2] / "observability"


def _load_yaml(path: Path) -> dict:
    """Load and parse a YAML file, failing the test on syntax errors."""
    assert path.exists(), f"Config file missing: {path}"
    with open(path) as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict), f"Expected top-level dict in {path.name}"
    return data


class TestPrometheusConfig:
    """Validate observability/prometheus/prometheus.yml."""

    def test_file_parseable(self) -> None:
        _load_yaml(OBSERVABILITY_DIR / "prometheus" / "prometheus.yml")

    def test_has_scrape_configs(self) -> None:
        data = _load_yaml(OBSERVABILITY_DIR / "prometheus" / "prometheus.yml")
        assert "scrape_configs" in data
        assert len(data["scrape_configs"]) >= 1

    def test_nexus_job_configured(self) -> None:
        data = _load_yaml(OBSERVABILITY_DIR / "prometheus" / "prometheus.yml")
        job_names = [sc["job_name"] for sc in data["scrape_configs"]]
        assert "nexus" in job_names


class TestLokiConfig:
    """Validate observability/loki/loki.yml."""

    def test_file_parseable(self) -> None:
        _load_yaml(OBSERVABILITY_DIR / "loki" / "loki.yml")

    def test_has_server(self) -> None:
        data = _load_yaml(OBSERVABILITY_DIR / "loki" / "loki.yml")
        assert "server" in data

    def test_has_schema_config(self) -> None:
        data = _load_yaml(OBSERVABILITY_DIR / "loki" / "loki.yml")
        assert "schema_config" in data

    def test_auth_disabled(self) -> None:
        data = _load_yaml(OBSERVABILITY_DIR / "loki" / "loki.yml")
        assert data.get("auth_enabled") is False


class TestTempoConfig:
    """Validate observability/tempo/tempo.yml."""

    def test_file_parseable(self) -> None:
        _load_yaml(OBSERVABILITY_DIR / "tempo" / "tempo.yml")

    def test_has_distributor_with_otlp(self) -> None:
        data = _load_yaml(OBSERVABILITY_DIR / "tempo" / "tempo.yml")
        assert "distributor" in data
        receivers = data["distributor"]["receivers"]
        assert "otlp" in receivers

    def test_otlp_grpc_and_http(self) -> None:
        data = _load_yaml(OBSERVABILITY_DIR / "tempo" / "tempo.yml")
        protocols = data["distributor"]["receivers"]["otlp"]["protocols"]
        assert "grpc" in protocols
        assert "http" in protocols


class TestDatasourcesConfig:
    """Validate observability/grafana/provisioning/datasources/datasources.yml."""

    def test_file_parseable(self) -> None:
        _load_yaml(
            OBSERVABILITY_DIR / "grafana" / "provisioning" / "datasources" / "datasources.yml"
        )

    def test_has_three_datasources(self) -> None:
        data = _load_yaml(
            OBSERVABILITY_DIR / "grafana" / "provisioning" / "datasources" / "datasources.yml"
        )
        assert "datasources" in data
        names = {ds["name"] for ds in data["datasources"]}
        assert names == {"Prometheus", "Loki", "Tempo"}

    def test_prometheus_is_default(self) -> None:
        data = _load_yaml(
            OBSERVABILITY_DIR / "grafana" / "provisioning" / "datasources" / "datasources.yml"
        )
        prom = next(ds for ds in data["datasources"] if ds["name"] == "Prometheus")
        assert prom.get("isDefault") is True


class TestPostgresExporterConfig:
    """Validate postgres scrape job in prometheus.yml (Issue #762)."""

    def test_postgres_job_in_prometheus_config(self) -> None:
        data = _load_yaml(OBSERVABILITY_DIR / "prometheus" / "prometheus.yml")
        job_names = [sc["job_name"] for sc in data["scrape_configs"]]
        assert "postgres" in job_names

    def test_postgres_scrape_interval(self) -> None:
        data = _load_yaml(OBSERVABILITY_DIR / "prometheus" / "prometheus.yml")
        pg_job = next(sc for sc in data["scrape_configs"] if sc["job_name"] == "postgres")
        assert pg_job["scrape_interval"] == "30s"


class TestDashboardJson:
    """Validate pg-stat-statements.json dashboard (Issue #762)."""

    DASHBOARD_PATH = (
        OBSERVABILITY_DIR / "grafana" / "provisioning" / "dashboards" / "pg-stat-statements.json"
    )

    def test_dashboard_json_parseable(self) -> None:
        import json

        assert self.DASHBOARD_PATH.exists(), f"Dashboard file missing: {self.DASHBOARD_PATH}"
        with open(self.DASHBOARD_PATH) as f:
            data = json.load(f)
        assert isinstance(data, dict)

    def test_dashboard_has_required_fields(self) -> None:
        import json

        with open(self.DASHBOARD_PATH) as f:
            data = json.load(f)
        assert "uid" in data
        assert "title" in data
        assert "panels" in data
        assert len(data["panels"]) > 0

    def test_dashboard_panels_reference_valid_datasource(self) -> None:
        import json

        with open(self.DASHBOARD_PATH) as f:
            data = json.load(f)
        for panel in data["panels"]:
            ds = panel.get("datasource")
            if ds is None:
                continue  # row panels may lack a datasource
            assert ds.get("uid") == "prometheus", (
                f"Panel '{panel.get('title')}' uses datasource UID '{ds.get('uid')}', expected 'prometheus'"
            )


class TestAllYamlFilesParseable:
    """Smoke test: every .yml file under observability/ must be valid YAML."""

    @pytest.mark.parametrize(
        "yaml_file",
        sorted(OBSERVABILITY_DIR.rglob("*.yml")),
        ids=lambda p: str(p.relative_to(OBSERVABILITY_DIR)),
    )
    def test_parseable(self, yaml_file: Path) -> None:
        with open(yaml_file) as f:
            data = yaml.safe_load(f)
        assert data is not None, f"{yaml_file.name} parsed to None"
