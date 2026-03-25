"""Highest Response Ratio Next (HRRN) scoring (Issue #1274).

Pure functions for HRRN priority computation:
  score = (wait_time + estimated_service_time) / estimated_service_time

This favors short jobs while preventing starvation of long jobs,
as their response ratio grows with wait time.

No I/O, no side effects — suitable for Hypothesis property-based testing.
"""

from nexus.services.scheduler.constants import DEFAULT_EST_SERVICE_TIME_SECS


def compute_hrrn_score(
    wait_seconds: float,
    estimated_service_time: float = DEFAULT_EST_SERVICE_TIME_SECS,
) -> float:
    """Compute HRRN score for a single task.

    Args:
        wait_seconds: Time spent waiting in the queue (seconds).
        estimated_service_time: Estimated execution time (seconds).

    Returns:
        HRRN score >= 1.0.

    Raises:
        ValueError: If estimated_service_time <= 0.
    """
    if estimated_service_time <= 0:
        raise ValueError(f"estimated_service_time must be > 0, got {estimated_service_time}")
    wait = max(0.0, wait_seconds)
    return (wait + estimated_service_time) / estimated_service_time


def rank_by_hrrn(
    tasks: list[dict],
    now_epoch: float,
    *,
    enqueued_key: str = "enqueued_at_epoch",
    service_time_key: str = "estimated_service_time",
) -> list[dict]:
    """Rank tasks by HRRN score (descending — highest ratio first).

    Returns a new sorted list; input is not mutated.

    Args:
        tasks: List of task dicts with enqueued timestamp and service time.
        now_epoch: Current time as Unix epoch seconds.
        enqueued_key: Dict key for the enqueue epoch timestamp.
        service_time_key: Dict key for estimated service time.

    Returns:
        New list sorted by HRRN score descending.
    """

    def _score(task: dict) -> float:
        enqueued: float = task.get(enqueued_key, now_epoch)
        est: float = task.get(service_time_key, DEFAULT_EST_SERVICE_TIME_SECS)
        wait = max(0.0, now_epoch - enqueued)
        if est <= 0:
            return float("inf")
        return (wait + est) / est

    return sorted(tasks, key=_score, reverse=True)
