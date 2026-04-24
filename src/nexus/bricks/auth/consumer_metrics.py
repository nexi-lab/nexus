"""Prometheus metrics for the read path (#3818).

Low-cardinality labels only:
  - provider ∈ {aws, github}
  - result ∈ {ok, stale, denied, invalid_token, envelope_error}
  - cache ∈ {hit, miss}
  - reason (cache evictions) ∈ {ttl, lru, expires_at}
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

TOKEN_EXCHANGE_REQUESTS = Counter(
    "nexus_token_exchange_requests_total",
    "RFC 8693 token-exchange requests by outcome",
    labelnames=("provider", "result"),
)

TOKEN_EXCHANGE_LATENCY = Histogram(
    "nexus_token_exchange_latency_seconds",
    "Latency of /v1/auth/token-exchange end-to-end",
    labelnames=("provider", "cache"),
)

CONSUMER_CACHE_SIZE = Gauge(
    "nexus_consumer_cache_size",
    "Current entries in ResolvedCredCache",
)

CONSUMER_CACHE_EVICTIONS = Counter(
    "nexus_consumer_cache_evictions_total",
    "ResolvedCredCache evictions by reason",
    labelnames=("reason",),
)

READ_AUDIT_WRITES = Counter(
    "nexus_read_audit_writes_total",
    "Auth-profile-read audit rows written",
    labelnames=("cache",),
)
