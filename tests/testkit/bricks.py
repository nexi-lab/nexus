"""Fake bricks and lifecycle probes for Nexus tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from inspect import isawaitable
from typing import Any

__all__ = [
    "FakeLifecycleBrick",
    "FakeSearchBrick",
    "ServiceLifecycleProbe",
    "probe_service_lifecycle",
]


@dataclass(frozen=True, slots=True)
class ServiceLifecycleProbe:
    """Result from exercising a service or brick lifecycle."""

    name: str
    started_after_start: bool | None
    healthy_after_start: bool | None
    started_after_stop: bool | None
    healthy_after_stop: bool | None
    events: tuple[str, ...] = ()


async def _maybe_await(value: Any) -> Any:
    if isawaitable(value):
        return await value
    return value


async def _call_first(service: Any, names: tuple[str, ...]) -> str:
    for name in names:
        method = getattr(service, name, None)
        if callable(method):
            await _maybe_await(method())
            return name
    raise AttributeError(f"{type(service).__name__} has none of: {', '.join(names)}")


def _started_state(service: Any) -> bool | None:
    for name in ("started", "is_initialized", "is_running"):
        value = getattr(service, name, None)
        if value is not None and not callable(value):
            return bool(value)
    return None


def _coerce_health(value: Any) -> bool:
    if isinstance(value, Mapping):
        if "healthy" in value:
            return bool(value["healthy"])
        status = str(value.get("status", "")).lower()
        if status:
            return status in {"healthy", "ok", "ready", "running"}
        if "initialized" in value:
            return bool(value["initialized"])
    return bool(value)


async def _health_state(service: Any) -> bool | None:
    for name in ("health_check", "get_health"):
        method = getattr(service, name, None)
        if callable(method):
            return _coerce_health(await _maybe_await(method()))
    return None


def _event_log(service: Any) -> tuple[str, ...]:
    events = getattr(service, "events", ())
    return tuple(events)


async def probe_service_lifecycle(
    service: Any, *, name: str | None = None
) -> ServiceLifecycleProbe:
    """Start, health-check, and stop a service/brick with common lifecycle names."""

    await _call_first(service, ("start", "startup", "initialize"))
    started_after_start = _started_state(service)
    healthy_after_start = await _health_state(service)

    await _call_first(service, ("stop", "shutdown", "close"))
    started_after_stop = _started_state(service)
    healthy_after_stop = await _health_state(service)

    return ServiceLifecycleProbe(
        name=name or getattr(service, "name", type(service).__name__),
        started_after_start=started_after_start,
        healthy_after_start=healthy_after_start,
        started_after_stop=started_after_stop,
        healthy_after_stop=healthy_after_stop,
        events=_event_log(service),
    )


class FakeLifecycleBrick:
    """Small async lifecycle fake for brick and background-service tests."""

    def __init__(self, *, name: str = "fake", healthy: bool = True) -> None:
        self.name = name
        self.healthy = healthy
        self.started = False
        self.start_calls = 0
        self.stop_calls = 0
        self._events: list[str] = []

    @property
    def events(self) -> tuple[str, ...]:
        return tuple(self._events)

    @property
    def is_initialized(self) -> bool:
        return self.started

    async def start(self) -> None:
        self.start_calls += 1
        if self.started:
            return
        self.started = True
        self._events.append("start")

    async def stop(self) -> None:
        self.stop_calls += 1
        if not self.started:
            return
        self.started = False
        self._events.append("stop")

    async def startup(self) -> None:
        await self.start()

    async def shutdown(self) -> None:
        await self.stop()

    async def initialize(self) -> None:
        await self.start()

    async def close(self) -> None:
        await self.stop()

    async def health_check(self) -> bool:
        return self.started and self.healthy


class FakeSearchBrick(FakeLifecycleBrick):
    """Search brick fake that satisfies `SearchBrickProtocol` structurally."""

    def __init__(
        self,
        *,
        results: Sequence[Any] = (),
        name: str = "search",
        healthy: bool = True,
    ) -> None:
        super().__init__(name=name, healthy=healthy)
        self._results = tuple(results)
        self._queries: list[dict[str, Any]] = []
        self._notifications: list[tuple[str, str]] = []

    @property
    def queries(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._queries)

    @property
    def notifications(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._notifications)

    async def search(
        self,
        query: str,
        search_type: str = "hybrid",
        limit: int = 10,
        path_filter: str | None = None,
        alpha: float = 0.5,
        fusion_method: str = "rrf",
        adaptive_k: bool = False,
        zone_id: str | None = None,
    ) -> list[Any]:
        self._queries.append(
            {
                "query": query,
                "search_type": search_type,
                "limit": limit,
                "path_filter": path_filter,
                "alpha": alpha,
                "fusion_method": fusion_method,
                "adaptive_k": adaptive_k,
                "zone_id": zone_id,
            }
        )
        return list(self._results[:limit])

    def get_stats(self) -> dict[str, Any]:
        return {
            "queries": len(self._queries),
            "notifications": len(self._notifications),
            "started": self.started,
        }

    def get_health(self) -> dict[str, Any]:
        return {
            "status": "healthy" if self.started and self.healthy else "unhealthy",
            "initialized": self.is_initialized,
        }

    async def notify_file_change(self, path: str, change_type: str = "update") -> None:
        self._notifications.append((path, change_type))
