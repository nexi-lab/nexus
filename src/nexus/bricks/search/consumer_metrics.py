"""Prometheus metrics for search mutation consumers (#4337).

Low-cardinality labels only:
  - consumer ∈ {bm25, fts, embedding, txtai}
  - kind ∈ {permanent, transient}

Split of responsibilities: the parking gate in ``daemon.py`` increments
``MUTATION_PARKED_TOTAL`` / ``MUTATION_UNRESOLVED_RETRIES_TOTAL``; the
``MutationParkStore`` owns ``MUTATION_PARKED`` (gauge synced on load /
park / remove) and ``MUTATION_PARKED_EVICTED_TOTAL``.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge

MUTATION_PARKED_TOTAL = Counter(
    "nexus_search_mutation_parked_total",
    "Search mutation events parked after exhausting their retry budget",
    labelnames=("consumer", "kind"),
)

MUTATION_PARKED = Gauge(
    "nexus_search_mutation_parked_current",
    "Search mutation events currently parked",
    labelnames=("consumer",),
)

MUTATION_UNRESOLVED_RETRIES_TOTAL = Counter(
    "nexus_search_mutation_unresolved_retries_total",
    "Retry passes that observed an unresolved mutation (early warning before parking)",
    labelnames=("consumer", "kind"),
)

MUTATION_PARKED_EVICTED_TOTAL = Counter(
    "nexus_search_mutation_parked_evicted_total",
    "Parked entries evicted by the per-consumer cap",
    labelnames=("consumer",),
)
