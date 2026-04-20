"""Tests for src/nexus/server/api/v1/routers/token_exchange.py."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.v1.routers.token_exchange import make_token_exchange_router


@pytest.fixture
def app_flag_off() -> FastAPI:
    a = FastAPI()
    a.include_router(make_token_exchange_router(enabled=False))
    return a


@pytest.fixture
def app_flag_on() -> FastAPI:
    a = FastAPI()
    a.include_router(make_token_exchange_router(enabled=True))
    return a


def _rfc8693_payload() -> dict[str, str]:
    return {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": "some-daemon-jwt",
        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
        "resource": "urn:nexus:gmail",
    }


def test_flag_off_returns_501(app_flag_off: FastAPI) -> None:
    client = TestClient(app_flag_off)
    r = client.post("/v1/auth/token-exchange", json=_rfc8693_payload())
    assert r.status_code == 501, r.text
    assert "deferred" in r.json()["detail"]
    assert "#3788" in r.json()["detail"]


def test_flag_on_still_returns_501_with_schema(app_flag_on: FastAPI) -> None:
    client = TestClient(app_flag_on)
    r = client.post("/v1/auth/token-exchange", json=_rfc8693_payload())
    assert r.status_code == 501, r.text
    assert "deferred" in r.json()["detail"]


def test_missing_body_field_422(app_flag_off: FastAPI) -> None:
    client = TestClient(app_flag_off)
    # Drop the required ``subject_token`` field — Pydantic validation fires
    # before the handler, so we get 422 not 501 even with the flag off.
    payload = _rfc8693_payload()
    del payload["subject_token"]
    r = client.post("/v1/auth/token-exchange", json=payload)
    assert r.status_code == 422, r.text
