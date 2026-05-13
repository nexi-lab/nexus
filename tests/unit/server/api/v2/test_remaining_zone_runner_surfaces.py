from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.v2.routers.credentials import router as credentials_router
from nexus.server.api.v2.routers.events_replay import router as events_router
from nexus.server.api.v2.routers.events_replay import watch_router
from nexus.server.api.v2.routers.subscriptions import router as subscriptions_router
from nexus.server.dependencies import require_auth

T = TypeVar("T")


class RecordingRunner:
    async def call(self, work: Callable[[], Awaitable[T]]) -> T:
        return await work()


class RecordingRegistry:
    def __init__(self) -> None:
        self.zones: list[str] = []

    def runner_for(self, zone_id: str) -> RecordingRunner:
        self.zones.append(zone_id)
        return RecordingRunner()


class _Dumpable:
    def __init__(self, **values: Any) -> None:
        self._values = values

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:  # noqa: ARG002
        return dict(self._values)


class FakeSubscriptionManager:
    def create(self, *, zone_id: str, data: Any, created_by: str | None) -> _Dumpable:  # noqa: ARG002
        return _Dumpable(
            subscription_id="sub-1",
            zone_id=zone_id,
            created_by=created_by,
            enabled=True,
        )


class _Event:
    def to_dict(self) -> dict[str, Any]:
        return {"event_id": "evt-1", "zone_id": "eng"}


class _ReplayResult:
    events = [_Event()]
    next_cursor = None
    has_more = False


class FakeReplayService:
    def replay(self, **kwargs: Any) -> _ReplayResult:
        assert kwargs["zone_id"] == "eng"
        return _ReplayResult()


class FakeWatchFs:
    def sys_watch(
        self, path: str, timeout: float, *, recursive: bool, context: Any
    ) -> dict[str, Any]:
        assert path == "/workspace"
        assert timeout == 0.1
        assert recursive is False
        assert context.zone_id == "eng"
        return {"path": path, "type": "write"}


class _CredentialStatus:
    credential_id = "cred-1"
    issuer_did = "did:nexus:issuer"
    subject_did = "did:nexus:alice"
    is_active = True
    created_at = None
    expires_at = None
    revoked_at = None
    delegation_depth = 0


class FakeCredentialService:
    def list_agent_credentials(
        self, agent_id: str, *, active_only: bool
    ) -> list[_CredentialStatus]:
        assert agent_id == "alice"
        assert active_only is True
        return [_CredentialStatus()]


def _auth() -> dict[str, Any]:
    return {
        "authenticated": True,
        "subject_type": "user",
        "subject_id": "alice",
        "zone_id": "eng",
        "zone_perms": [["eng", "rw"]],
        "is_admin": False,
    }


def test_create_subscription_runs_in_auth_zone_runner() -> None:
    registry = RecordingRegistry()
    app = FastAPI()
    app.state.subscription_manager = FakeSubscriptionManager()
    app.state.zone_registry = registry
    app.dependency_overrides[require_auth] = _auth
    app.include_router(subscriptions_router)

    with TestClient(app) as client:
        response = client.post(
            "/api/v2/subscriptions",
            json={"event_types": ["file_write"], "url": "https://example.com/hook"},
        )

    assert response.status_code == 201
    assert response.json()["zone_id"] == "eng"
    assert registry.zones == ["eng"]


def test_replay_events_runs_in_requested_zone_runner() -> None:
    registry = RecordingRegistry()
    app = FastAPI()
    app.state.replay_service = FakeReplayService()
    app.state.zone_registry = registry
    app.dependency_overrides[require_auth] = _auth
    app.include_router(events_router)

    with TestClient(app) as client:
        response = client.get("/api/v2/events/replay", params={"zone_id": "eng"})

    assert response.status_code == 200
    assert response.json()["events"] == [{"event_id": "evt-1", "zone_id": "eng"}]
    assert registry.zones == ["eng"]


def test_watch_for_changes_runs_in_auth_zone_runner() -> None:
    registry = RecordingRegistry()
    app = FastAPI()
    app.state.nexus_fs = FakeWatchFs()
    app.state.zone_registry = registry
    app.dependency_overrides[require_auth] = _auth
    app.include_router(watch_router)

    with TestClient(app) as client:
        response = client.get("/api/v2/watch", params={"path": "/workspace", "timeout": 0.1})

    assert response.status_code == 200
    assert response.json() == {
        "changes": [{"path": "/workspace", "type": "write"}],
        "timeout": False,
    }
    assert registry.zones == ["eng"]


def test_list_agent_credentials_runs_in_auth_zone_runner() -> None:
    registry = RecordingRegistry()
    app = FastAPI()
    app.state.credential_service = FakeCredentialService()
    app.state.zone_registry = registry
    app.dependency_overrides[require_auth] = _auth
    app.include_router(credentials_router)

    with TestClient(app) as client:
        response = client.get("/api/v2/agents/alice/credentials")

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert registry.zones == ["eng"]
