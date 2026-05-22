"""Full-profile control-plane surface coverage for issue #4138."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from click.testing import CliRunner

REQUIRED_RPC_METHODS = {
    "admin_write_permission",
    "admin_create_key",
    "admin_list_keys",
    "admin_get_key",
    "admin_revoke_key",
    "admin_update_key",
    "provision_user",
    "deprovision_user",
    "audit_list",
    "audit_export",
    "events_replay",
    "governance_alerts",
    "governance_rings",
    "governance_status",
    "federation_client_whoami",
    "federation_export_zone",
    "federation_import_zone",
    "federation_create_zone",
    "federation_remove_zone",
    "federation_join",
    "federation_mount",
    "federation_unmount",
    "federation_share",
    "federation_list_zones",
    "federation_cluster_info",
    "pay_balance",
    "pay_transfer",
    "pay_history",
}


ADMIN_ONLY_RPC_METHODS = {
    "admin_write_permission",
    "admin_create_key",
    "admin_list_keys",
    "admin_get_key",
    "admin_revoke_key",
    "admin_update_key",
    "provision_user",
    "deprovision_user",
    "audit_list",
    "audit_export",
    "events_replay",
    "governance_alerts",
    "governance_rings",
    "governance_status",
    "federation_export_zone",
    "federation_import_zone",
    "federation_create_zone",
    "federation_remove_zone",
    "federation_join",
    "federation_mount",
    "federation_unmount",
    "federation_share",
}


PUBLIC_RPC_METHODS = {
    "federation_client_whoami",
    "federation_list_zones",
    "federation_cluster_info",
    "pay_balance",
    "pay_transfer",
    "pay_history",
}


def _iter_rpc_methods(surfaces: Iterable[Any]) -> set[str]:
    return {method for surface in surfaces for method in surface.rpc_methods}


def test_control_plane_matrix_covers_issue_4138_rpc_methods() -> None:
    from nexus.contracts.control_plane_coverage import CONTROL_PLANE_SURFACES

    covered = _iter_rpc_methods(CONTROL_PLANE_SURFACES)
    assert covered >= REQUIRED_RPC_METHODS


def test_control_plane_matrix_tracks_profile_gate_and_gap_status() -> None:
    from nexus.contracts.control_plane_coverage import CONTROL_PLANE_SURFACES

    for surface in CONTROL_PLANE_SURFACES:
        assert surface.profile == "full"
        assert surface.module_group
        assert surface.how_to_use
        assert surface.correctness_tests
        assert surface.performance_classification
        assert surface.gap_issue is None or surface.gap_issue.startswith("#")


def test_server_discovers_user_provisioning_service_for_rpc() -> None:
    from nexus.server.fastapi_server import DISCOVERABLE_RPC_SERVICE_NAMES

    assert "user_provisioning" in DISCOVERABLE_RPC_SERVICE_NAMES


def test_cli_surface_contains_required_operator_commands() -> None:
    from nexus.cli.commands.admin import admin
    from nexus.cli.commands.audit import audit
    from nexus.cli.commands.events_cli import events
    from nexus.cli.commands.federation import federation
    from nexus.cli.commands.governance_cli import governance
    from nexus.cli.commands.pay import pay

    assert {
        "create-user",
        "create-key",
        "create-agent-key",
        "list-users",
        "get-user",
        "revoke-key",
        "update-key",
        "provision-user",
        "deprovision-user",
    } <= set(admin.commands)
    assert {"list", "export"} <= set(audit.commands)
    assert {"replay"} <= set(events.commands)
    assert {"status", "alerts", "rings"} <= set(governance.commands)
    assert {"status", "zones", "info", "mount", "unmount"} <= set(federation.commands)
    assert {"balance", "transfer", "history"} <= set(pay.commands)


def test_sensitive_rpc_services_mark_admin_only() -> None:
    from nexus.server.rpc.services.audit_rpc import AuditRPCService
    from nexus.server.rpc.services.events_rpc import EventsRPCService
    from nexus.server.rpc.services.federation_rpc import FederationRPCMixin, FederationRPCService
    from nexus.server.rpc.services.governance_rpc import GovernanceRPCService
    from nexus.server.rpc.services.pay_rpc import PayRPCService
    from nexus.services.lifecycle.user_provisioning import UserProvisioningService

    service_classes = (
        AuditRPCService,
        EventsRPCService,
        FederationRPCMixin,
        FederationRPCService,
        GovernanceRPCService,
        PayRPCService,
        UserProvisioningService,
    )
    exposed: dict[str, Any] = {}
    for cls in service_classes:
        for name in dir(cls):
            attr = getattr(cls, name)
            if callable(attr) and getattr(attr, "_rpc_exposed", False):
                exposed[getattr(attr, "_rpc_name", name)] = attr

    for method in ADMIN_ONLY_RPC_METHODS - {
        "admin_write_permission",
        "admin_create_key",
        "admin_list_keys",
        "admin_get_key",
        "admin_revoke_key",
        "admin_update_key",
    }:
        assert getattr(exposed[method], "_rpc_admin_only", None) is True, method

    for method in PUBLIC_RPC_METHODS:
        assert getattr(exposed[method], "_rpc_admin_only", None) is False, method


def test_audit_router_requires_auth_but_not_admin() -> None:
    from nexus.server.api.v2.routers.pay import audit_router
    from nexus.server.dependencies import require_admin, require_auth

    dependencies = [dep.dependency for dep in audit_router.dependencies]

    assert require_auth in dependencies
    assert require_admin not in dependencies


def test_audit_http_endpoints_use_exchange_audit_log_and_verify_tamper(tmp_path) -> None:
    import asyncio

    from sqlalchemy import create_engine, update
    from sqlalchemy.orm import sessionmaker

    from nexus.server.api.v2.routers.pay import list_audit_transactions, verify_audit_integrity
    from nexus.storage.exchange_audit_logger import ExchangeAuditLogger
    from nexus.storage.models import Base
    from nexus.storage.models.exchange_audit_log import ExchangeAuditLogModel

    engine = create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    class FakeRecordStore:
        pass

    record_store = FakeRecordStore()
    record_store.session_factory = session_factory
    audit_logger = ExchangeAuditLogger(record_store=record_store)

    record_id = audit_logger.record(
        protocol="x402",
        buyer_agent_id="buyer",
        seller_agent_id="seller",
        amount=Decimal("10"),
        currency="credits",
        status="settled",
        application="gateway",
        zone_id="root",
        transfer_id="tb_1",
    )

    listed = asyncio.run(
        list_audit_transactions(
            protocol="x402",
            include_total=True,
            record_store=record_store,
        )
    )
    assert listed["total"] == 1
    assert listed["transactions"][0]["id"] == record_id
    assert listed["transactions"][0]["record_hash"]

    with session_factory() as session:
        session.execute(
            update(ExchangeAuditLogModel)
            .where(ExchangeAuditLogModel.id == record_id)
            .values(amount=Decimal("99"))
        )
        session.commit()

    integrity = asyncio.run(verify_audit_integrity(record_id, record_store=record_store))
    assert integrity == {
        "record_id": record_id,
        "is_valid": False,
        "record_hash": listed["transactions"][0]["record_hash"],
    }


def test_pay_rpc_service_autodetects_tigerbeetle_like_rest(monkeypatch) -> None:
    from nexus.server.fastapi_server import _pay_rpc_credits_enabled

    class FakeSocket:
        def close(self) -> None:
            pass

    monkeypatch.delenv("NEXUS_PAY_ENABLED", raising=False)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    monkeypatch.setattr("socket.gethostbyname", lambda hostname: "127.0.0.1")
    monkeypatch.setattr("socket.create_connection", lambda address, timeout: FakeSocket())

    assert _pay_rpc_credits_enabled() is True


def test_pay_rpc_service_stays_disabled_without_tigerbeetle_module(monkeypatch) -> None:
    from nexus.server.fastapi_server import _pay_rpc_credits_enabled

    class FakeSocket:
        def close(self) -> None:
            pass

    monkeypatch.delenv("NEXUS_PAY_ENABLED", raising=False)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    monkeypatch.setattr("socket.gethostbyname", lambda hostname: "127.0.0.1")
    monkeypatch.setattr("socket.create_connection", lambda address, timeout: FakeSocket())

    assert _pay_rpc_credits_enabled() is False


def test_audit_export_calls_audit_logger_with_keyword_filters() -> None:
    from nexus.server.rpc.services.audit_rpc import AuditRPCService

    class FakeAuditLogger:
        def __init__(self) -> None:
            self.filters: dict[str, Any] | None = None

        def iter_transactions(self, *, filters: dict[str, Any] | None = None):
            self.filters = filters
            return []

    audit_logger = FakeAuditLogger()
    result = AuditRPCService(audit_logger).audit_export(fmt="json", since="2026-01-01")

    assert result == "[]"
    assert audit_logger.filters == {"since": "2026-01-01"}


def test_events_replay_adapts_sync_replay_result() -> None:
    import asyncio

    from nexus.server.rpc.services.events_rpc import EventsRPCService
    from nexus.services.event_log.replay import EventRecord, ReplayResult

    class FakeReplayService:
        def replay(self, **kwargs: Any) -> ReplayResult:
            assert kwargs["limit"] == 1
            assert kwargs["since_timestamp"] == datetime(2026, 1, 1, tzinfo=UTC)
            return ReplayResult(
                events=[
                    EventRecord(
                        event_id="evt_1",
                        type="write",
                        path="/doc.md",
                        new_path=None,
                        zone_id="root",
                        agent_id=None,
                        status="ok",
                        delivered=True,
                        timestamp="2026-01-01T00:00:00+00:00",
                        sequence_number=1,
                    )
                ],
                next_cursor="cursor",
                has_more=True,
            )

    result = asyncio.run(
        EventsRPCService(FakeReplayService()).events_replay(
            since="2026-01-01T00:00:00+00:00",
            limit=1,
        )
    )

    assert result["has_more"] is True
    assert result["next_cursor"] == "cursor"
    assert result["events"][0]["event_id"] == "evt_1"


def test_governance_rpc_uses_zone_scoped_async_services() -> None:
    import asyncio

    from nexus.bricks.governance.models import AnomalySeverity
    from nexus.server.rpc.services.governance_rpc import GovernanceRPCService

    @dataclass(frozen=True)
    class Alert:
        alert_id: str = "alert_1"
        agent_id: str = "agent_1"
        severity: str = "high"
        details: str = "flagged"
        created_at: str = "2026-01-01"

    @dataclass(frozen=True)
    class Ring:
        ring_id: str = "ring_1"
        agents: list[str] | None = None
        confidence: float = 0.9
        detected_at: str = "2026-01-01"

    class FakeAnomalyService:
        async def get_alerts(
            self,
            *,
            zone_id: str,
            severity: AnomalySeverity | None = None,
        ) -> list[Alert]:
            assert zone_id == "root"
            assert severity in (None, AnomalySeverity.HIGH)
            return [Alert()]

    class FakeCollusionService:
        async def detect_rings(self, *, zone_id: str) -> list[Ring]:
            assert zone_id == "root"
            return [Ring(agents=["a", "b", "c"])]

    service = GovernanceRPCService(FakeAnomalyService(), FakeCollusionService())

    alerts = asyncio.run(service.governance_alerts(severity="high"))
    rings = asyncio.run(service.governance_rings())
    status = asyncio.run(service.governance_status())

    assert alerts == {
        "alerts": [
            {
                "alert_id": "alert_1",
                "agent_id": "agent_1",
                "severity": "high",
                "description": "flagged",
                "created_at": "2026-01-01",
            }
        ],
        "count": 1,
    }
    assert rings["count"] == 1
    assert status["recent_alerts"]["count"] == 1
    assert status["fraud_rings"]["count"] == 1


def test_governance_rpc_degrades_when_optional_storage_is_unavailable() -> None:
    import asyncio

    from nexus.server.rpc.services.governance_rpc import GovernanceRPCService

    class MissingAnomalyService:
        async def get_alerts(self, **kwargs: Any) -> list[Any]:
            raise RuntimeError("no such table: governance_anomaly_alerts")

    class MissingCollusionService:
        async def detect_rings(self, **kwargs: Any) -> list[Any]:
            raise RuntimeError("networkx is not installed")

    service = GovernanceRPCService(MissingAnomalyService(), MissingCollusionService())

    assert asyncio.run(service.governance_alerts()) == {"alerts": [], "count": 0}
    assert asyncio.run(service.governance_rings()) == {"rings": [], "count": 0}
    assert asyncio.run(service.governance_status()) == {
        "recent_alerts": {"alerts": [], "count": 0},
        "fraud_rings": {"rings": [], "count": 0},
    }


def test_admin_provision_user_cli_calls_rpc(monkeypatch) -> None:
    from nexus.cli.commands.admin import admin

    calls: list[tuple[str, dict[str, Any] | None]] = []

    def fake_get_admin_rpc(url: str | None, api_key: str | None):
        assert url == "http://server"
        assert api_key == "adminkey"

        def call_rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            calls.append((method, params))
            return {
                "user_id": "alice",
                "zone_id": "team",
                "key_id": "kid_1",
                "workspace_path": "/zone/team/user/alice/workspace/ws_1",
                "agent_paths": [],
                "created_resources": {"user": True},
            }

        return call_rpc

    monkeypatch.setattr("nexus.cli.commands.admin.get_admin_rpc", fake_get_admin_rpc)

    result = CliRunner().invoke(
        admin,
        [
            "provision-user",
            "alice",
            "alice@example.com",
            "--display-name",
            "Alice",
            "--zone-id",
            "team",
            "--api-key-name",
            "Alice laptop",
            "--no-agents",
            "--json",
            "--remote-url",
            "http://server",
            "--remote-api-key",
            "adminkey",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert calls == [
        (
            "provision_user",
            {
                "user_id": "alice",
                "email": "alice@example.com",
                "create_api_key": True,
                "create_agents": False,
                "import_skills": False,
                "display_name": "Alice",
                "zone_id": "team",
                "api_key_name": "Alice laptop",
            },
        )
    ]


def test_admin_deprovision_user_cli_calls_rpc(monkeypatch) -> None:
    from nexus.cli.commands.admin import admin

    calls: list[tuple[str, dict[str, Any] | None]] = []

    def fake_get_admin_rpc(url: str | None, api_key: str | None):
        assert url == "http://server"
        assert api_key == "adminkey"

        def call_rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            calls.append((method, params))
            return {
                "user_id": "alice",
                "zone_id": "team",
                "deleted_directories": ["/zone/team/user/alice/workspace"],
                "deleted_api_keys": 1,
                "deleted_permissions": 2,
                "user_record_deleted": True,
            }

        return call_rpc

    monkeypatch.setattr("nexus.cli.commands.admin.get_admin_rpc", fake_get_admin_rpc)

    result = CliRunner().invoke(
        admin,
        [
            "deprovision-user",
            "alice",
            "--zone-id",
            "team",
            "--delete-user-record",
            "--force",
            "--json",
            "--remote-url",
            "http://server",
            "--remote-api-key",
            "adminkey",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert calls == [
        (
            "deprovision_user",
            {
                "user_id": "alice",
                "delete_user_record": True,
                "force": True,
                "zone_id": "team",
            },
        )
    ]
