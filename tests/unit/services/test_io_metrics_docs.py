from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DOC = ROOT / "docs" / "operations" / "nexus-io-observability.md"
DASHBOARD = (
    ROOT
    / "observability"
    / "grafana"
    / "provisioning"
    / "dashboards"
    / "nexus-io-observability.json"
)

METRIC_NAMES = [
    "nexus_cache_requests_total",
    "nexus_cache_hit_ratio",
    "nexus_cache_evictions_total",
    "nexus_cache_bytes_in_use",
    "nexus_cache_admission_rejected_total",
    "nexus_cache_etag_revalidate_total",
    "nexus_prefetch_issued_bytes_total",
    "nexus_prefetch_used_bytes_total",
    "nexus_prefetch_wasted_bytes_total",
    "nexus_prefetch_window_size",
    "nexus_prefetch_pattern_detected_total",
    "nexus_read_latency_seconds",
    "nexus_read_bytes_total",
    "nexus_read_batch_size",
    "nexus_fuse_passthrough_used_total",
    "nexus_write_coalesce_flush_total",
    "nexus_write_coalesce_dirty_bytes",
    "nexus_write_backend_rpc_total",
    "nexus_generation_mismatch_total",
    "nexus_etag_check_total",
]


def test_io_observability_docs_cover_every_metric() -> None:
    body = DOC.read_text()
    for metric in METRIC_NAMES:
        assert metric in body


def test_io_observability_docs_describe_scraping_and_reserved_series() -> None:
    body = DOC.read_text()
    assert "nexus-server:2026" in body
    assert "`localhost:2026` only when Prometheus runs on the host" in body
    assert "127.0.0.1:9464 must be reachable" in body
    assert "daemon" in body
    assert "--metrics-addr" in body
    assert "NEXUS_FUSE_METRICS_ADDR" in body
    assert "remain zero or absent until their labeled series are first observed" in body


def test_grafana_dashboard_is_valid_and_mentions_core_metrics() -> None:
    dashboard = json.loads(DASHBOARD.read_text())
    assert dashboard["uid"] == "nexus-io-observability"
    assert dashboard["title"] == "Nexus I/O Observability"

    expressions = [
        target["expr"] for panel in dashboard["panels"] for target in panel.get("targets", [])
    ]
    encoded = "\n".join(expressions)
    for metric in [
        "nexus_read_latency_seconds",
        "nexus_read_bytes_total",
        "nexus_cache_requests_total",
        "nexus_cache_bytes_in_use",
        "nexus_etag_check_total",
        "nexus_read_batch_size",
        "nexus_write_backend_rpc_total",
        "nexus_prefetch_issued_bytes_total",
        "nexus_prefetch_used_bytes_total",
        "nexus_prefetch_wasted_bytes_total",
        "nexus_write_coalesce_flush_total",
    ]:
        assert metric in encoded


def test_grafana_cache_hit_percentage_uses_low_traffic_safe_denominator() -> None:
    dashboard = json.loads(DASHBOARD.read_text())
    hit_panel = next(
        panel for panel in dashboard["panels"] if panel["title"] == "Derived Cache Hit Percentage"
    )
    expr = hit_panel["targets"][0]["expr"]

    assert "clamp_min(sum by (tier) (rate(nexus_cache_requests_total[5m])), 1)" not in expr
    assert "or on (tier) 0 * sum by (tier)" in expr
    assert "clamp_min(sum by (tier) (rate(nexus_cache_requests_total[5m])), 1e-9)" in expr


def test_grafana_read_latency_panel_includes_quantiles_by_tier() -> None:
    dashboard = json.loads(DASHBOARD.read_text())
    latency_panel = next(
        panel for panel in dashboard["panels"] if panel["title"] == "Read Latency By Tier"
    )

    expected_targets = {
        "p50 {{tier}}": "histogram_quantile(0.50, sum by (le, tier) (rate(nexus_read_latency_seconds_bucket[5m])))",
        "p95 {{tier}}": "histogram_quantile(0.95, sum by (le, tier) (rate(nexus_read_latency_seconds_bucket[5m])))",
        "p99 {{tier}}": "histogram_quantile(0.99, sum by (le, tier) (rate(nexus_read_latency_seconds_bucket[5m])))",
    }
    actual_targets = {target["legendFormat"]: target["expr"] for target in latency_panel["targets"]}

    assert actual_targets == expected_targets


def test_grafana_cache_bytes_panel_aggregates_by_tier() -> None:
    dashboard = json.loads(DASHBOARD.read_text())
    cache_bytes_panel = next(
        panel for panel in dashboard["panels"] if panel["title"] == "Cache Bytes In Use"
    )
    target = cache_bytes_panel["targets"][0]

    assert target["expr"] == "sum by (tier) (nexus_cache_bytes_in_use)"
    assert target["legendFormat"] == "{{tier}}"


def test_grafana_backend_write_rpc_rate_is_aggregated() -> None:
    dashboard = json.loads(DASHBOARD.read_text())
    write_panel = next(
        panel for panel in dashboard["panels"] if panel["title"] == "Backend Write RPC Rate"
    )
    target = write_panel["targets"][0]

    assert target["expr"] == "sum(rate(nexus_write_backend_rpc_total[5m]))"
    assert target["legendFormat"] == "total write RPC/s"


def test_grafana_prefetch_panel_shows_reserved_efficiency_inputs() -> None:
    dashboard = json.loads(DASHBOARD.read_text())
    prefetch_panel = next(
        panel
        for panel in dashboard["panels"]
        if panel["title"] == "Prefetch And Coalescing Reserved"
    )
    targets = {target["legendFormat"]: target["expr"] for target in prefetch_panel["targets"]}

    assert targets["prefetch issued B/s"] == "rate(nexus_prefetch_issued_bytes_total[5m])"
    assert targets["prefetch used B/s"] == "rate(nexus_prefetch_used_bytes_total[5m])"
    assert targets["prefetch wasted B/s"] == "rate(nexus_prefetch_wasted_bytes_total[5m])"
    assert targets["flush {{trigger}}"] == (
        "sum by (trigger) (rate(nexus_write_coalesce_flush_total[5m]))"
    )
