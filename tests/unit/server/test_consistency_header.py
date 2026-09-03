"""``X-Nexus-Consistency`` header → ``OperationContext.consistency`` (Issue #4739)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from nexus.server import dependencies
from nexus.server.dependencies import get_operation_context, parse_consistency_header


class TestParseConsistencyHeader:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, None),
            ("", None),
            ("   ", None),
            ("strong", "strong"),
            ("STRONG", "strong"),
            ("  Strong ", "strong"),
            ("eventual", "eventual"),
        ],
    )
    def test_valid_values(self, raw, expected):
        assert parse_consistency_header(raw) == expected

    @pytest.mark.parametrize("raw", ["bounded", "fully_consistent", "yes", "1"])
    def test_invalid_values_are_400(self, raw):
        with pytest.raises(HTTPException) as exc_info:
            parse_consistency_header(raw)
        assert exc_info.value.status_code == 400
        assert "X-Nexus-Consistency" in str(exc_info.value.detail)


class TestOperationContextMapping:
    def test_consistency_is_carried_from_auth_result(self):
        ctx = get_operation_context(
            {
                "subject_type": "user",
                "subject_id": "alice",
                "zone_id": "root",
                "consistency": "strong",
            }
        )
        assert ctx.consistency == "strong"
        assert ctx.user_id == "alice"

    def test_default_is_none(self):
        ctx = get_operation_context({"subject_type": "user", "subject_id": "alice"})
        assert ctx.consistency is None


class TestGetAuthResultHeader:
    """End-to-end through the FastAPI dependency with a stubbed resolver."""

    @pytest.fixture
    def shared_auth(self) -> dict[str, Any]:
        # One dict instance returned for every call — models a cached auth result.
        return {
            "authenticated": True,
            "is_admin": False,
            "subject_type": "user",
            "subject_id": "alice",
            "zone_id": "root",
        }

    @pytest.fixture
    def client(self, monkeypatch, shared_auth):
        async def _fake_resolve_auth(**_kwargs: Any) -> dict[str, Any]:
            return shared_auth

        monkeypatch.setattr(dependencies, "resolve_auth", _fake_resolve_auth)

        app = FastAPI()

        @app.get("/ctx")
        async def _ctx(auth: dict[str, Any] = Depends(dependencies.require_auth)) -> dict[str, Any]:
            return {"consistency": get_operation_context(auth).consistency}

        return TestClient(app)

    def test_strong_header_reaches_operation_context(self, client):
        resp = client.get("/ctx", headers={"X-Nexus-Consistency": "strong"})
        assert resp.status_code == 200
        assert resp.json() == {"consistency": "strong"}

    def test_header_is_case_insensitive(self, client):
        resp = client.get("/ctx", headers={"x-nexus-consistency": "Strong"})
        assert resp.status_code == 200
        assert resp.json() == {"consistency": "strong"}

    def test_absent_header_means_eventual(self, client):
        resp = client.get("/ctx")
        assert resp.status_code == 200
        assert resp.json() == {"consistency": None}

    def test_invalid_header_is_400(self, client):
        resp = client.get("/ctx", headers={"X-Nexus-Consistency": "bounded"})
        assert resp.status_code == 400
        assert "X-Nexus-Consistency" in resp.json()["detail"]

    def test_cached_auth_dict_is_not_mutated(self, client, shared_auth):
        client.get("/ctx", headers={"X-Nexus-Consistency": "strong"})
        assert "consistency" not in shared_auth
        # A following request without the header must not inherit strong.
        assert client.get("/ctx").json() == {"consistency": None}
