"""Prometheus metrics for envelope encryption (issue #3803).

Low-cardinality labels only: tenant_id is acceptable (single-digit tenants at
this scale); principal_id and profile_id are NOT labels — they'd explode the
time-series count.

Metrics:
  - auth_dek_cache_hits_total{tenant_id}
  - auth_dek_cache_misses_total{tenant_id}
  - auth_dek_unwrap_errors_total{tenant_id,error_class}
  - auth_dek_unwrap_latency_seconds{tenant_id}
  - auth_kek_rotate_rows_total{tenant_id,from_version,to_version}
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

DEK_CACHE_HITS = Counter(
    "auth_dek_cache_hits_total",
    "Number of DEK cache hits on the decrypt path.",
    labelnames=["tenant_id"],
)

DEK_CACHE_MISSES = Counter(
    "auth_dek_cache_misses_total",
    "Number of DEK cache misses (KMS/Vault round-trip required).",
    labelnames=["tenant_id"],
)

DEK_UNWRAP_ERRORS = Counter(
    "auth_dek_unwrap_errors_total",
    "EncryptionProvider.unwrap_dek failures.",
    labelnames=["tenant_id", "error_class"],
)

DEK_UNWRAP_LATENCY = Histogram(
    "auth_dek_unwrap_latency_seconds",
    "Time spent in EncryptionProvider.unwrap_dek.",
    labelnames=["tenant_id"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

KEK_ROTATE_ROWS = Counter(
    "auth_kek_rotate_rows_total",
    "Rows rewrapped to a new kek_version by rotate_kek_for_tenant.",
    labelnames=["tenant_id", "from_version", "to_version"],
)

__all__ = [
    "DEK_CACHE_HITS",
    "DEK_CACHE_MISSES",
    "DEK_UNWRAP_ERRORS",
    "DEK_UNWRAP_LATENCY",
    "KEK_ROTATE_ROWS",
]
