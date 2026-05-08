from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from nexus.bricks.pay.credits import CreditsError, InsufficientCreditsError
from nexus.server.api.v2.routers import pay as pay_router_module
from nexus.server.api.v2.routers.pay import router as pay_router
from nexus.storage.models.payments import CreditReservationMeta, PaymentTransactionMeta, UsageEvent
from nexus.storage.record_store import SQLAlchemyRecordStore


class DummyCredits:
    DISABLED_UNLIMITED_BALANCE = Decimal("999999999")

    def __init__(self) -> None:
        self.balance_calls: list[tuple[str, str]] = []
        self.transfer_calls: list[tuple[str, str, Decimal, str | None, str]] = []
        self.batch_transfer_calls: list[tuple[int, str]] = []
        self.reserve_calls: list[tuple[str, Decimal, int, str]] = []
        self.commit_calls: list[tuple[str, Decimal | None]] = []
        self.release_calls: list[str] = []
        self.deduct_calls: list[tuple[str, Decimal, str]] = []
        self.transfer_error: Exception | None = None
        self.batch_error: Exception | None = None
        self.reserve_error: Exception | None = None

    async def get_balance(self, agent_id: str, zone_id: str = "root") -> Decimal:
        self.balance_calls.append((agent_id, zone_id))
        return Decimal("100.00")

    async def get_balance_with_reserved(
        self, agent_id: str, zone_id: str = "root"
    ) -> tuple[Decimal, Decimal]:
        self.balance_calls.append((agent_id, zone_id))
        return Decimal("98.75"), Decimal("1.25")

    async def transfer(
        self,
        from_id: str,
        to_id: str,
        amount: Decimal,
        *,
        memo: str = "",
        idempotency_key: str | None = None,
        zone_id: str = "root",
    ) -> str:
        if self.transfer_error is not None:
            raise self.transfer_error
        self.transfer_calls.append((from_id, to_id, amount, idempotency_key, zone_id))
        return "tb-transfer-id"

    async def transfer_batch(self, transfers: list[Any], *, zone_id: str = "root") -> list[str]:
        if self.batch_error is not None:
            raise self.batch_error
        self.batch_transfer_calls.append((len(transfers), zone_id))
        return [str(1000 + i) for i, _transfer in enumerate(transfers)]

    async def reserve(
        self,
        agent_id: str,
        amount: Decimal,
        timeout_seconds: int = 300,
        *,
        zone_id: str = "root",
    ) -> str:
        if self.reserve_error is not None:
            raise self.reserve_error
        self.reserve_calls.append((agent_id, amount, timeout_seconds, zone_id))
        return "123456789"

    async def commit_reservation(
        self, reservation_id: str, actual_amount: Decimal | None = None
    ) -> None:
        self.commit_calls.append((reservation_id, actual_amount))

    async def release_reservation(self, reservation_id: str) -> None:
        self.release_calls.append(reservation_id)

    async def deduct_fast(
        self, agent_id: str, amount: Decimal, *, zone_id: str = "root", **_: Any
    ) -> bool:
        self.deduct_calls.append((agent_id, amount, zone_id))
        return True


def _build_client(tmp_path) -> tuple[TestClient, DummyCredits, SQLAlchemyRecordStore]:
    app = FastAPI()
    app.state.api_key = "secret"
    app.state.auth_provider = None
    record_store = SQLAlchemyRecordStore(db_path=tmp_path / "pay.db", create_tables=True)
    app.state.record_store = record_store
    credits = DummyCredits()
    app.dependency_overrides[pay_router_module._get_credits_service] = lambda: credits
    app.include_router(pay_router)
    return TestClient(app), credits, record_store


def _build_provider_client(tmp_path) -> tuple[TestClient, DummyCredits, SQLAlchemyRecordStore]:
    class UserAuthProvider:
        async def authenticate(self, _token: str) -> SimpleNamespace:
            return SimpleNamespace(
                authenticated=True,
                is_admin=False,
                subject_type="user",
                subject_id="user-alice",
                zone_id="zone-a",
                inherit_permissions=True,
                metadata={},
            )

    app = FastAPI()
    app.state.api_key = None
    app.state.auth_provider = UserAuthProvider()
    record_store = SQLAlchemyRecordStore(db_path=tmp_path / "pay-provider.db", create_tables=True)
    app.state.record_store = record_store
    credits = DummyCredits()
    app.dependency_overrides[pay_router_module._get_credits_service] = lambda: credits
    app.include_router(pay_router)
    return TestClient(app), credits, record_store


def _headers(agent_id: str = "agent-alice", zone_id: str = "zone-a") -> dict[str, str]:
    return {
        "Authorization": "Bearer secret",
        "X-Agent-ID": agent_id,
        "X-Nexus-Zone-ID": zone_id,
    }


def test_transfer_requires_auth_and_uses_authenticated_actor_with_decimal_micro_units(tmp_path):
    client, credits, record_store = _build_client(tmp_path)

    unauth = client.post("/api/v2/pay/transfer", json={"to": "agent-bob", "amount": "2.55"})
    assert unauth.status_code == 401

    response = client.post(
        "/api/v2/pay/transfer",
        json={
            "to": "agent-bob",
            "amount": "2.55",
            "memo": "work",
            "idempotency_key": "idem-1",
        },
        headers=_headers(),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["from_agent"] == "agent-alice"
    assert body["amount"] == "2.55"
    assert credits.transfer_calls == [
        ("agent-alice", "agent-bob", Decimal("2.55"), "idem-1", "zone-a")
    ]

    with record_store.session_factory() as session:
        txn = session.scalar(select(PaymentTransactionMeta))
        assert txn is not None
        assert txn.from_agent_id == "agent-alice"
        assert txn.zone_id == "zone-a"
        assert txn.amount == 2_550_000


def test_non_admin_user_cannot_debit_x_agent_id_wallet(tmp_path):
    client, credits, _record_store = _build_provider_client(tmp_path)

    response = client.post(
        "/api/v2/pay/transfer",
        json={"to": "agent-bob", "amount": "2.55"},
        headers={
            "Authorization": "Bearer provider-token",
            "X-Agent-ID": "agent-victim",
            "X-Nexus-Zone-ID": "zone-a",
        },
    )

    assert response.status_code == 201
    assert response.json()["from_agent"] == "user-alice"
    assert credits.transfer_calls == [("user-alice", "agent-bob", Decimal("2.55"), None, "zone-a")]


def test_can_afford_returns_contract_amount_for_authenticated_actor(tmp_path):
    client, credits, _record_store = _build_client(tmp_path)

    response = client.get(
        "/api/v2/pay/can-afford?amount=4.25",
        headers=_headers(agent_id="agent-pay"),
    )

    assert response.status_code == 200
    assert response.json() == {"can_afford": True, "amount": "4.25"}
    assert credits.balance_calls == [("agent-pay", "zone-a")]


def test_batch_transfer_and_meter_routes_match_exchange_contract(tmp_path):
    client, credits, record_store = _build_client(tmp_path)

    batch = client.post(
        "/api/v2/pay/transfer/batch",
        json={
            "transfers": [
                {"to": "agent-bob", "amount": "1.25", "memo": "a"},
                {"to": "agent-carol", "amount": "0.001", "memo": "b"},
            ]
        },
        headers=_headers(),
    )

    assert batch.status_code == 201
    receipts = batch.json()
    assert [r["from_agent"] for r in receipts] == ["agent-alice", "agent-alice"]
    assert [r["amount"] for r in receipts] == ["1.25", "0.001"]
    assert credits.batch_transfer_calls == [(2, "zone-a")]

    meter = client.post(
        "/api/v2/pay/meter",
        json={"amount": "0.001", "event_type": "api_call"},
        headers=_headers(),
    )

    assert meter.status_code == 200
    assert meter.json() == {"success": True}
    assert credits.deduct_calls == [("agent-alice", Decimal("0.001"), "zone-a")]

    with record_store.session_factory() as session:
        usages = session.scalars(select(UsageEvent)).all()
        assert len(usages) == 1
        assert usages[0].agent_id == "agent-alice"
        assert usages[0].amount == 1_000


def test_empty_batch_transfer_matches_sdk_noop_contract(tmp_path):
    client, credits, record_store = _build_client(tmp_path)

    response = client.post(
        "/api/v2/pay/transfer/batch",
        json={"transfers": []},
        headers=_headers(),
    )

    assert response.status_code == 201
    assert response.json() == []
    assert credits.batch_transfer_calls == [(0, "zone-a")]
    with record_store.session_factory() as session:
        assert session.scalar(select(func.count(PaymentTransactionMeta.id))) == 0


def test_batch_transfer_ledger_failure_returns_error_without_sql_receipts(tmp_path):
    client, credits, record_store = _build_client(tmp_path)
    credits.batch_error = CreditsError("Batch transfer failed")

    response = client.post(
        "/api/v2/pay/transfer/batch",
        json={"transfers": [{"to": "agent-bob", "amount": "1.25"}]},
        headers=_headers(),
    )

    assert response.status_code == 502
    assert "Batch transfer failed" in response.json()["detail"]
    with record_store.session_factory() as session:
        assert session.scalar(select(func.count(PaymentTransactionMeta.id))) == 0


def test_reservation_uses_owner_timeout_task_and_enforces_owner_on_commit(tmp_path):
    client, credits, record_store = _build_client(tmp_path)

    created = client.post(
        "/api/v2/pay/reserve",
        json={"amount": "5.50", "timeout": 600, "purpose": "gpu", "task_id": "task-1"},
        headers=_headers(agent_id="agent-alice"),
    )

    assert created.status_code == 201
    reservation_id = created.json()["id"]
    assert credits.reserve_calls == [("agent-alice", Decimal("5.50"), 600, "zone-a")]

    forbidden = client.post(
        f"/api/v2/pay/reserve/{reservation_id}/commit",
        json={"actual_amount": "4.25"},
        headers=_headers(agent_id="agent-bob"),
    )
    assert forbidden.status_code == 403

    committed = client.post(
        f"/api/v2/pay/reserve/{reservation_id}/commit",
        json={"actual_amount": "4.25"},
        headers=_headers(agent_id="agent-alice"),
    )
    assert committed.status_code == 204
    assert credits.commit_calls == [(reservation_id, Decimal("4.25"))]

    with record_store.session_factory() as session:
        reservation = session.scalar(select(CreditReservationMeta))
        assert reservation is not None
        assert reservation.agent_id == "agent-alice"
        assert reservation.zone_id == "zone-a"
        assert reservation.amount == 4_250_000
        assert reservation.task_id == "task-1"
        assert reservation.status == "committed"


def test_reservation_ledger_failure_returns_error_without_sql_reservation(tmp_path):
    client, credits, record_store = _build_client(tmp_path)
    credits.reserve_error = InsufficientCreditsError("Insufficient balance to reserve")

    response = client.post(
        "/api/v2/pay/reserve",
        json={"amount": "5.50", "timeout": 600, "purpose": "gpu"},
        headers=_headers(),
    )

    assert response.status_code == 402
    assert "Insufficient balance" in response.json()["detail"]
    with record_store.session_factory() as session:
        assert session.scalar(select(func.count(CreditReservationMeta.id))) == 0
