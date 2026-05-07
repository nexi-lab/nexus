from __future__ import annotations

from prometheus_client import REGISTRY

from nexus.lib import io_metrics


def _sample(name: str, **labels: str) -> float:
    for family in REGISTRY.collect():
        for sample in family.samples:
            if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items()):
                return float(sample.value)
    return 0.0


def test_record_cache_request_increments_bounded_counter() -> None:
    before = _sample("nexus_cache_requests_total", tier="sqlite", result="hit")
    io_metrics.record_cache_request("sqlite", "hit")
    after = _sample("nexus_cache_requests_total", tier="sqlite", result="hit")
    assert after == before + 1


def test_unknown_cache_labels_collapse_to_other() -> None:
    before = _sample("nexus_cache_requests_total", tier="other", result="other")
    io_metrics.record_cache_request("tenant-123", "path-/secret")
    after = _sample("nexus_cache_requests_total", tier="other", result="other")
    assert after == before + 1


def test_cache_gauges_and_future_counters_update() -> None:
    io_metrics.set_cache_hit_ratio("sqlite", 0.75)
    assert _sample("nexus_cache_hit_ratio", tier="sqlite") == 0.75

    io_metrics.set_cache_bytes_in_use("sqlite", 1234)
    assert _sample("nexus_cache_bytes_in_use", tier="sqlite") == 1234

    before_evictions = _sample("nexus_cache_evictions_total", tier="sqlite", reason="capacity")
    io_metrics.record_cache_eviction("sqlite", "capacity")
    after_evictions = _sample("nexus_cache_evictions_total", tier="sqlite", reason="capacity")
    assert after_evictions == before_evictions + 1

    before_rejected = _sample("nexus_cache_admission_rejected_total")
    io_metrics.record_cache_admission_rejected()
    after_rejected = _sample("nexus_cache_admission_rejected_total")
    assert after_rejected == before_rejected + 1


def test_cache_hit_ratio_clamps_to_valid_range() -> None:
    io_metrics.set_cache_hit_ratio("dram", -0.25)
    assert _sample("nexus_cache_hit_ratio", tier="dram") == 0.0

    io_metrics.set_cache_hit_ratio("dram", 1.25)
    assert _sample("nexus_cache_hit_ratio", tier="dram") == 1.0


def test_negative_byte_gauge_and_histogram_inputs_clamp_to_zero() -> None:
    before_issued = _sample("nexus_prefetch_issued_bytes_total")
    io_metrics.record_prefetch_issued(-10)
    assert _sample("nexus_prefetch_issued_bytes_total") == before_issued

    before_used = _sample("nexus_prefetch_used_bytes_total")
    io_metrics.record_prefetch_used(-7)
    assert _sample("nexus_prefetch_used_bytes_total") == before_used

    before_wasted = _sample("nexus_prefetch_wasted_bytes_total")
    io_metrics.record_prefetch_wasted(-3)
    assert _sample("nexus_prefetch_wasted_bytes_total") == before_wasted

    io_metrics.set_cache_bytes_in_use("sqlite", -1234)
    assert _sample("nexus_cache_bytes_in_use", tier="sqlite") == 0.0

    before_read_bytes = _sample("nexus_read_bytes_total", tier="backend")
    before_read_count = _sample("nexus_read_latency_seconds_count", tier="backend")
    before_read_sum = _sample("nexus_read_latency_seconds_sum", tier="backend")
    io_metrics.record_read(tier="backend", bytes_read=-512, latency_seconds=-0.005)
    assert _sample("nexus_read_bytes_total", tier="backend") == before_read_bytes
    assert _sample("nexus_read_latency_seconds_count", tier="backend") == before_read_count + 1
    assert _sample("nexus_read_latency_seconds_sum", tier="backend") == before_read_sum

    before_batch_count = _sample("nexus_read_batch_size_count")
    before_batch_sum = _sample("nexus_read_batch_size_sum")
    io_metrics.record_read_batch_size(-4)
    assert _sample("nexus_read_batch_size_count") == before_batch_count + 1
    assert _sample("nexus_read_batch_size_sum") == before_batch_sum

    io_metrics.set_prefetch_window_size(-4096, mount="root", workspace="default")
    assert _sample("nexus_prefetch_window_size", mount="root", workspace="default") == 0.0

    io_metrics.set_write_coalesce_dirty_bytes(-2048)
    assert _sample("nexus_write_coalesce_dirty_bytes") == 0.0


def test_labeled_gauge_recorders_collapse_to_bounded_labels() -> None:
    io_metrics.set_cache_hit_ratio("tenant-123", 0.5)
    assert _sample("nexus_cache_hit_ratio", tier="other") == 0.5

    io_metrics.set_cache_bytes_in_use("path-/secret", 123)
    assert _sample("nexus_cache_bytes_in_use", tier="other") == 123

    io_metrics.set_prefetch_window_size(2048, mount="tenant-123", workspace="path-/secret")
    assert _sample("nexus_prefetch_window_size", mount="default", workspace="default") == 2048


def test_etag_recorders_update_expected_results() -> None:
    before_revalidate = _sample("nexus_cache_etag_revalidate_total", result="304")
    io_metrics.record_cache_etag_revalidate("304")
    after_revalidate = _sample("nexus_cache_etag_revalidate_total", result="304")
    assert after_revalidate == before_revalidate + 1

    before_check = _sample("nexus_etag_check_total", result="updated")
    io_metrics.record_etag_check("updated")
    after_check = _sample("nexus_etag_check_total", result="updated")
    assert after_check == before_check + 1


def test_prefetch_recorders_update_bounded_metrics() -> None:
    before_issued = _sample("nexus_prefetch_issued_bytes_total")
    io_metrics.record_prefetch_issued(10)
    assert _sample("nexus_prefetch_issued_bytes_total") == before_issued + 10

    before_used = _sample("nexus_prefetch_used_bytes_total")
    io_metrics.record_prefetch_used(7)
    assert _sample("nexus_prefetch_used_bytes_total") == before_used + 7

    before_wasted = _sample("nexus_prefetch_wasted_bytes_total")
    io_metrics.record_prefetch_wasted(3)
    assert _sample("nexus_prefetch_wasted_bytes_total") == before_wasted + 3

    io_metrics.set_prefetch_window_size(4096, mount="root", workspace="default")
    assert _sample("nexus_prefetch_window_size", mount="root", workspace="default") == 4096

    before_pattern = _sample("nexus_prefetch_pattern_detected_total", pattern="sequential")
    io_metrics.record_prefetch_pattern("sequential")
    after_pattern = _sample("nexus_prefetch_pattern_detected_total", pattern="sequential")
    assert after_pattern == before_pattern + 1


def test_read_metrics_update_bytes_and_histogram_count() -> None:
    before_bytes = _sample("nexus_read_bytes_total", tier="backend")
    before_count = _sample("nexus_read_latency_seconds_count", tier="backend")
    io_metrics.record_read(tier="backend", bytes_read=512, latency_seconds=0.005)
    assert _sample("nexus_read_bytes_total", tier="backend") == before_bytes + 512
    assert _sample("nexus_read_latency_seconds_count", tier="backend") == before_count + 1


def test_read_bytes_only_metric_does_not_observe_latency() -> None:
    before_bytes = _sample("nexus_read_bytes_total", tier="batch")
    before_count = _sample("nexus_read_latency_seconds_count", tier="batch")

    io_metrics.record_read_bytes(tier="batch", bytes_read=512)

    assert _sample("nexus_read_bytes_total", tier="batch") == before_bytes + 512
    assert _sample("nexus_read_latency_seconds_count", tier="batch") == before_count


def test_batch_write_and_consistency_recorders_update() -> None:
    before_batch = _sample("nexus_read_batch_size_count")
    io_metrics.record_read_batch_size(4)
    assert _sample("nexus_read_batch_size_count") == before_batch + 1

    before_passthrough = _sample("nexus_fuse_passthrough_used_total")
    io_metrics.record_fuse_passthrough_used()
    assert _sample("nexus_fuse_passthrough_used_total") == before_passthrough + 1

    before_flush = _sample("nexus_write_coalesce_flush_total", trigger="time")
    io_metrics.record_write_coalesce_flush("time")
    assert _sample("nexus_write_coalesce_flush_total", trigger="time") == before_flush + 1

    io_metrics.set_write_coalesce_dirty_bytes(2048)
    assert _sample("nexus_write_coalesce_dirty_bytes") == 2048

    before_rpc = _sample("nexus_write_backend_rpc_total")
    io_metrics.record_write_backend_rpc()
    assert _sample("nexus_write_backend_rpc_total") == before_rpc + 1

    before_mismatch = _sample("nexus_generation_mismatch_total")
    io_metrics.record_generation_mismatch()
    assert _sample("nexus_generation_mismatch_total") == before_mismatch + 1


def test_unknown_labeled_recorders_collapse_to_other() -> None:
    before_eviction = _sample("nexus_cache_evictions_total", tier="other", reason="other")
    io_metrics.record_cache_eviction("tenant-123", "path-/secret")
    after_eviction = _sample("nexus_cache_evictions_total", tier="other", reason="other")
    assert after_eviction == before_eviction + 1

    before_revalidate = _sample("nexus_cache_etag_revalidate_total", result="other")
    io_metrics.record_cache_etag_revalidate("path-/secret")
    after_revalidate = _sample("nexus_cache_etag_revalidate_total", result="other")
    assert after_revalidate == before_revalidate + 1

    before_check = _sample("nexus_etag_check_total", result="other")
    io_metrics.record_etag_check("tenant-123")
    after_check = _sample("nexus_etag_check_total", result="other")
    assert after_check == before_check + 1

    before_pattern = _sample("nexus_prefetch_pattern_detected_total", pattern="other")
    io_metrics.record_prefetch_pattern("customer-path")
    after_pattern = _sample("nexus_prefetch_pattern_detected_total", pattern="other")
    assert after_pattern == before_pattern + 1

    before_read_bytes = _sample("nexus_read_bytes_total", tier="other")
    before_read_count = _sample("nexus_read_latency_seconds_count", tier="other")
    io_metrics.record_read(tier="tenant-123", bytes_read=8, latency_seconds=0.001)
    assert _sample("nexus_read_bytes_total", tier="other") == before_read_bytes + 8
    assert _sample("nexus_read_latency_seconds_count", tier="other") == before_read_count + 1

    before_flush = _sample("nexus_write_coalesce_flush_total", trigger="other")
    io_metrics.record_write_coalesce_flush("path-/secret")
    after_flush = _sample("nexus_write_coalesce_flush_total", trigger="other")
    assert after_flush == before_flush + 1
