from __future__ import annotations

from prometheus_client import generate_latest

from nexus.lib import io_metrics


def test_io_metrics_exposed_via_global_registry() -> None:
    io_metrics.record_read(tier="backend", bytes_read=1, latency_seconds=0.001)
    io_metrics.record_cache_request("sqlite", "hit")
    io_metrics.record_write_backend_rpc()

    body = generate_latest().decode()

    expected_names = [
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
    for name in expected_names:
        assert name in body
