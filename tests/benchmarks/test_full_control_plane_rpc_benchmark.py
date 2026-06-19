"""Full-profile control-plane RPC benchmarks (#4201).

These benchmarks provide the performance evidence required by the full-profile
control-plane story (#4138). Most paths use direct handler/service calls with
representative seeded data so the suite is CI-friendly and does not require
external services. Federation uses the real local kernel runtime when present
and skips clearly when the binary is unavailable.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.server.rpc.handlers.admin import (
    handle_admin_create_key,
    handle_admin_get_key,
    handle_admin_list_keys,
    handle_admin_revoke_key,
    handle_admin_update_key,
)
from nexus.server.rpc.services.audit_rpc import AuditRPCService
from nexus.server.rpc.services.events_rpc import EventsRPCService
from nexus.server.rpc.services.federation_rpc import FederationRPCService
from nexus.server.rpc.services.governance_rpc import GovernanceRPCService
from nexus.storage.models import Base

pytestmark = [
    pytest.mark.benchmark_ci,
    pytest.mark.benchmark(group="full-control-plane-rpc", min_rounds=5, max_time=0.5),
]


@dataclass
class _AdminContext:
    is_admin: bool = True
    user: str = "bench-admin"
    zone_id: str = ROOT_ZONE_ID


class _Params(SimpleNamespace):
    """Attribute bag matching generated RPC params objects."""


_T = TypeVar("_T")


def _run_with_p50_ceiling(
    benchmark: Any,
    fn: Callable[[], _T],
    *,
    p50_ceiling_ms: float,
) -> _T:
    result = benchmark(fn)
    stats = getattr(benchmark, "stats", None)
    if not stats:
        return result

    if isinstance(stats, dict):
        median_seconds = stats.get("median")
    else:
        median_seconds = getattr(getattr(stats, "stats", None), "median", None)

    if median_seconds is None:
        return result

    observed_ms = median_seconds * 1000
    extra_info = getattr(benchmark, "extra_info", None)
    if isinstance(extra_info, dict):
        extra_info["p50_ceiling_ms"] = p50_ceiling_ms
        extra_info["p50_observed_ms"] = observed_ms

    assert observed_ms <= p50_ceiling_ms, (
        f"{getattr(benchmark, 'name', 'benchmark')} p50 {observed_ms:.3f}ms "
        f"exceeds {p50_ceiling_ms:.3f}ms ceiling"
    )
    return result


def _admin_create_params(*, name: str, user_id: str, zone_id: str = ROOT_ZONE_ID) -> _Params:
    return _Params(
        name=name,
        zone_id=zone_id,
        user_id=user_id,
        is_admin=False,
        expires_days=None,
        subject_type="user",
        subject_id=None,
        grants=None,
    )


@pytest.fixture()
def control_plane_auth_provider(tmp_path: Path) -> SimpleNamespace:
    db_path = tmp_path / "control_plane_rpc.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    from nexus.storage.models.auth import ZoneModel

    with session_factory() as session:
        session.add(ZoneModel(zone_id=ROOT_ZONE_ID, name=ROOT_ZONE_ID, phase="Active"))
        session.add(ZoneModel(zone_id="bench-zone", name="bench-zone", phase="Active"))
        session.commit()

    provider = SimpleNamespace(
        session_factory=session_factory,
        _record_store=SimpleNamespace(session_factory=session_factory, engine=engine),
    )
    yield provider
    engine.dispose()


@pytest.fixture()
def seeded_admin_key(control_plane_auth_provider: SimpleNamespace) -> str:
    result = handle_admin_create_key(
        control_plane_auth_provider,
        _admin_create_params(name="seeded-key", user_id="seeded-user"),
        _AdminContext(),
    )
    return str(result["key_id"])


def test_admin_create_key_rpc_benchmark(
    benchmark: Any,
    control_plane_auth_provider: SimpleNamespace,
) -> None:
    counter = itertools.count()

    def _create_key() -> dict[str, Any]:
        index = next(counter)
        result = handle_admin_create_key(
            control_plane_auth_provider,
            _admin_create_params(name=f"bench-create-{index}", user_id=f"user-{index}"),
            _AdminContext(),
        )
        assert {"key_id", "api_key", "user_id", "zone_id", "is_admin"} <= result.keys()
        assert result["api_key"].startswith("sk-")
        return result

    _run_with_p50_ceiling(benchmark, _create_key, p50_ceiling_ms=250.0)


def test_admin_list_keys_rpc_benchmark(
    benchmark: Any,
    control_plane_auth_provider: SimpleNamespace,
) -> None:
    for index in range(50):
        handle_admin_create_key(
            control_plane_auth_provider,
            _admin_create_params(name=f"bench-list-{index}", user_id=f"list-user-{index}"),
            _AdminContext(),
        )

    params = _Params(
        user_id=None,
        zone_id=ROOT_ZONE_ID,
        is_admin=None,
        include_revoked=False,
        include_expired=False,
        limit=25,
        offset=0,
    )

    def _list_keys() -> dict[str, Any]:
        result = handle_admin_list_keys(control_plane_auth_provider, params, _AdminContext())
        assert {"keys", "total"} <= result.keys()
        assert 1 <= len(result["keys"]) <= 25
        assert result["total"] >= 50
        return result

    _run_with_p50_ceiling(benchmark, _list_keys, p50_ceiling_ms=100.0)


def test_admin_get_key_rpc_benchmark(
    benchmark: Any,
    control_plane_auth_provider: SimpleNamespace,
    seeded_admin_key: str,
) -> None:
    params = _Params(key_id=seeded_admin_key, zone_id=ROOT_ZONE_ID)

    def _get_key() -> dict[str, Any]:
        result = handle_admin_get_key(control_plane_auth_provider, params, _AdminContext())
        assert {"key_id", "user_id", "name", "zone_id", "revoked", "last_used_at"} <= result.keys()
        assert result["key_id"] == seeded_admin_key
        return result

    _run_with_p50_ceiling(benchmark, _get_key, p50_ceiling_ms=25.0)


def test_admin_update_key_rpc_benchmark(
    benchmark: Any,
    control_plane_auth_provider: SimpleNamespace,
    seeded_admin_key: str,
) -> None:
    counter = itertools.count()

    def _update_key() -> dict[str, Any]:
        name = f"bench-updated-{next(counter)}"
        result = handle_admin_update_key(
            control_plane_auth_provider,
            _Params(
                key_id=seeded_admin_key,
                zone_id=ROOT_ZONE_ID,
                name=name,
                is_admin=None,
                expires_days=None,
            ),
            _AdminContext(),
        )
        assert {"success", "key_id", "name", "zone_id", "is_admin"} <= result.keys()
        assert result["success"] is True
        assert result["name"] == name
        return result

    _run_with_p50_ceiling(benchmark, _update_key, p50_ceiling_ms=250.0)


def test_admin_revoke_key_rpc_benchmark(
    benchmark: Any,
    control_plane_auth_provider: SimpleNamespace,
) -> None:
    counter = itertools.count()

    def _create_and_revoke_key() -> dict[str, Any]:
        index = next(counter)
        created = handle_admin_create_key(
            control_plane_auth_provider,
            _admin_create_params(name=f"bench-revoke-{index}", user_id=f"revoke-user-{index}"),
            _AdminContext(),
        )
        result = handle_admin_revoke_key(
            control_plane_auth_provider,
            _Params(key_id=created["key_id"], zone_id=ROOT_ZONE_ID),
            _AdminContext(),
        )
        assert result == {"success": True, "key_id": created["key_id"]}
        return result

    _run_with_p50_ceiling(benchmark, _create_and_revoke_key, p50_ceiling_ms=250.0)


class _AuditColumn:
    def __init__(self, name: str) -> None:
        self.name = name


class _AuditRow:
    __table__ = SimpleNamespace(
        columns=[
            _AuditColumn("transfer_id"),
            _AuditColumn("buyer_agent_id"),
            _AuditColumn("seller_agent_id"),
            _AuditColumn("amount"),
            _AuditColumn("currency"),
            _AuditColumn("status"),
            _AuditColumn("created_at"),
        ]
    )

    def __init__(self, index: int) -> None:
        self.transfer_id = f"bench-transfer-{index}"
        self.buyer_agent_id = f"buyer-{index % 5}"
        self.seller_agent_id = f"seller-{index % 7}"
        self.amount = Decimal(index + 1)
        self.currency = "credits"
        self.status = "settled"
        self.created_at = datetime(2026, 1, 1, tzinfo=UTC)

    def as_response(self) -> dict[str, Any]:
        return {
            "transfer_id": self.transfer_id,
            "buyer_agent_id": self.buyer_agent_id,
            "seller_agent_id": self.seller_agent_id,
            "amount": str(self.amount),
            "currency": self.currency,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
        }


class _FakeAuditLogger:
    def __init__(self, count: int = 100) -> None:
        self._rows = [_AuditRow(index) for index in range(count)]

    def list_transactions_cursor(
        self,
        *,
        filters: dict[str, Any],
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        assert isinstance(filters, dict)
        start = int(cursor or 0)
        rows = self._rows[start : start + limit]
        next_cursor = str(start + limit) if start + limit < len(self._rows) else None
        return {
            "transactions": [row.as_response() for row in rows],
            "next_cursor": next_cursor,
            "has_more": next_cursor is not None,
        }

    def iter_transactions(self, filters: dict[str, Any]) -> list[_AuditRow]:
        assert isinstance(filters, dict)
        return self._rows


@pytest.fixture()
def audit_rpc_service() -> AuditRPCService:
    return AuditRPCService(_FakeAuditLogger())


def test_audit_list_rpc_benchmark(benchmark: Any, audit_rpc_service: AuditRPCService) -> None:
    def _list_audit() -> dict[str, Any]:
        result = audit_rpc_service.audit_list(limit=25)
        assert {"transactions", "next_cursor", "has_more"} <= result.keys()
        assert len(result["transactions"]) == 25
        return result

    _run_with_p50_ceiling(benchmark, _list_audit, p50_ceiling_ms=1.0)


def test_audit_export_rpc_benchmark(benchmark: Any, audit_rpc_service: AuditRPCService) -> None:
    def _export_audit() -> str:
        payload = audit_rpc_service.audit_export(fmt="json")
        records = json.loads(payload)
        assert len(records) == 100
        assert {"transfer_id", "amount", "status"} <= records[0].keys()
        return payload

    _run_with_p50_ceiling(benchmark, _export_audit, p50_ceiling_ms=5.0)


class _ReplayEvent:
    def __init__(self, index: int) -> None:
        self.index = index

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": f"event-{self.index}",
            "event_type": "operation",
            "path": f"/workspace/file-{self.index}.md",
            "created_at": "2026-01-01T00:00:00+00:00",
        }


class _FakeReplayService:
    def replay(
        self,
        *,
        since_timestamp: str | None,
        event_types: list[str] | None,
        path_pattern: str | None,
        limit: int,
    ) -> list[_ReplayEvent]:
        assert since_timestamp is None
        assert event_types in (None, ["operation"])
        assert path_pattern is None
        return [_ReplayEvent(index) for index in range(limit)]


def test_events_replay_rpc_benchmark(
    benchmark: Any,
    benchmark_loop: asyncio.AbstractEventLoop,
) -> None:
    service = EventsRPCService(_FakeReplayService())

    def _replay_events() -> dict[str, Any]:
        result = benchmark_loop.run_until_complete(
            service.events_replay(event_type="operation", limit=25)
        )
        assert {"events", "has_more"} <= result.keys()
        assert len(result["events"]) == 25
        assert result["events"][0]["event_id"] == "event-0"
        return result

    _run_with_p50_ceiling(benchmark, _replay_events, p50_ceiling_ms=1.0)


@dataclass
class _Alert:
    alert_id: str
    agent_id: str
    severity: str
    details: str
    created_at: str


@dataclass
class _Ring:
    ring_id: str
    agents: list[str]
    confidence: float
    detected_at: str


class _FakeAnomalyService:
    def __init__(self) -> None:
        self._alerts = [
            _Alert(
                alert_id=f"alert-{index}",
                agent_id=f"agent-{index % 5}",
                severity="high" if index % 2 else "medium",
                details="synthetic governance alert",
                created_at="2026-01-01T00:00:00+00:00",
            )
            for index in range(20)
        ]

    async def get_alerts(self, *, zone_id: str, severity: Any | None = None) -> list[_Alert]:
        assert zone_id
        if severity is None:
            return self._alerts
        severity_value = getattr(severity, "value", str(severity))
        return [alert for alert in self._alerts if alert.severity == severity_value]


class _FakeCollusionService:
    async def detect_rings(self, *, zone_id: str) -> list[_Ring]:
        assert zone_id
        return [
            _Ring(
                ring_id=f"ring-{index}",
                agents=[f"agent-{index}", f"agent-{index + 1}"],
                confidence=0.75 + (index / 100),
                detected_at="2026-01-01T00:00:00+00:00",
            )
            for index in range(5)
        ]


@pytest.fixture()
def governance_rpc_service() -> GovernanceRPCService:
    return GovernanceRPCService(
        anomaly_service=_FakeAnomalyService(),
        collusion_service=_FakeCollusionService(),
    )


def test_governance_alerts_rpc_benchmark(
    benchmark: Any,
    benchmark_loop: asyncio.AbstractEventLoop,
    governance_rpc_service: GovernanceRPCService,
) -> None:
    def _alerts() -> dict[str, Any]:
        result = benchmark_loop.run_until_complete(
            governance_rpc_service.governance_alerts(severity="high", limit=5)
        )
        assert {"alerts", "count"} <= result.keys()
        assert result["count"] == 5
        return result

    _run_with_p50_ceiling(benchmark, _alerts, p50_ceiling_ms=1.0)


def test_governance_rings_rpc_benchmark(
    benchmark: Any,
    benchmark_loop: asyncio.AbstractEventLoop,
    governance_rpc_service: GovernanceRPCService,
) -> None:
    def _rings() -> dict[str, Any]:
        result = benchmark_loop.run_until_complete(governance_rpc_service.governance_rings())
        assert {"rings", "count"} <= result.keys()
        assert result["count"] == 5
        return result

    _run_with_p50_ceiling(benchmark, _rings, p50_ceiling_ms=1.0)


def test_governance_status_rpc_benchmark(
    benchmark: Any,
    benchmark_loop: asyncio.AbstractEventLoop,
    governance_rpc_service: GovernanceRPCService,
) -> None:
    def _status() -> dict[str, Any]:
        result = benchmark_loop.run_until_complete(governance_rpc_service.governance_status())
        assert {"recent_alerts", "fraud_rings"} <= result.keys()
        assert result["recent_alerts"]["count"] == 5
        assert result["fraud_rings"]["count"] == 5
        return result

    _run_with_p50_ceiling(benchmark, _status, p50_ceiling_ms=1.0)


def _resolve_federation_runtime_binary() -> str | None:
    configured = os.environ.get("NEXUS_KERNEL_BINARY")
    if configured:
        return configured if os.path.exists(configured) else shutil.which(configured)
    return shutil.which("nexus-cluster") or shutil.which("nexusd-cluster")


@pytest.fixture(scope="module")
def federation_runtime_service(tmp_path_factory: pytest.TempPathFactory) -> FederationRPCService:
    binary = _resolve_federation_runtime_binary()
    if not binary:
        pytest.skip(
            "federation runtime unavailable: set NEXUS_KERNEL_BINARY or install "
            "nexus-cluster/nexusd-cluster"
        )

    from nexus.remote.kernel_client import KernelClient

    kernel = KernelClient(
        metadata_path=str(tmp_path_factory.mktemp("control_plane_federation_runtime")),
        timeout=20.0,
    )
    try:
        kernel.open()
    except Exception as exc:
        pytest.skip(f"federation runtime unavailable: {exc}")

    yield FederationRPCService(kernel)
    kernel.close()


def test_federation_list_zones_runtime_benchmark(
    benchmark: Any,
    federation_runtime_service: FederationRPCService,
) -> None:
    def _list_zones() -> dict[str, Any]:
        result = federation_runtime_service.federation_list_zones()
        assert {"zones", "node_id"} <= result.keys()
        assert isinstance(result["zones"], list)
        return result

    _run_with_p50_ceiling(benchmark, _list_zones, p50_ceiling_ms=250.0)


def test_federation_create_zone_runtime_benchmark(
    benchmark: Any,
    federation_runtime_service: FederationRPCService,
) -> None:
    counter = itertools.count()

    def _create_zone() -> dict[str, Any]:
        zone_id = f"bench-zone-{next(counter)}"
        result = federation_runtime_service.federation_create_zone(zone_id)
        assert result == {"zone_id": zone_id}
        return result

    _run_with_p50_ceiling(benchmark, _create_zone, p50_ceiling_ms=250.0)
