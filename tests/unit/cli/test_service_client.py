"""Tests for NexusServiceClient — HTTP client for non-FS CLI commands.

Covers:
- Client initialization (base_url, auth headers, trailing slash stripping)
- Context manager lifecycle
- Request method (param filtering, error handling, 204 responses)
- Pay endpoints (balance, transfer, history)
- Audit endpoints (list, export)
- Lock endpoints (list, info, release)
- Governance endpoints (alerts, status, rings)
- Event endpoints (replay, subscribe)
- Snapshot endpoints (create, list, restore)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nexus.cli.client import NexusAPIError, NexusServiceClient

# ── Helpers ───────────────────────────────────────────────


def _make_response(
    status_code: int = 200,
    json_data: dict | None = None,
    text: str = "",
) -> MagicMock:
    """Build a mock httpx.Response with the given status and body."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.return_value = {}
    return resp


# ── Fixtures ──────────────────────────────────────────────


@pytest.fixture()
def client_and_mock():
    """Create a NexusServiceClient with a mocked underlying httpx.Client.

    Since httpx is lazily imported inside ``__init__``, we patch ``httpx.Client``
    at the module level so the lazy import picks up the mock.
    """
    mock_http_client = MagicMock()
    with patch("httpx.Client", return_value=mock_http_client):
        client = NexusServiceClient("http://localhost:2026", "test-key")
    yield client, mock_http_client
    client.close()


# ── TestNexusServiceClientInit ────────────────────────────


class TestNexusServiceClientInit:
    """Test client initialization."""

    def test_init_sets_base_url(self) -> None:
        with patch("httpx.Client") as mock_cls:
            mock_cls.return_value = MagicMock()
            client = NexusServiceClient("http://localhost:2026", "test-key")
            call_kwargs = mock_cls.call_args[1]
            assert call_kwargs["base_url"] == "http://localhost:2026"
            client.close()

    def test_init_strips_trailing_slash(self) -> None:
        with patch("httpx.Client") as mock_cls:
            mock_cls.return_value = MagicMock()
            client = NexusServiceClient("http://localhost:2026/", "key")
            call_kwargs = mock_cls.call_args[1]
            assert call_kwargs["base_url"] == "http://localhost:2026"
            client.close()

    def test_init_strips_multiple_trailing_slashes(self) -> None:
        with patch("httpx.Client") as mock_cls:
            mock_cls.return_value = MagicMock()
            client = NexusServiceClient("http://localhost:2026///", "key")
            call_kwargs = mock_cls.call_args[1]
            assert call_kwargs["base_url"] == "http://localhost:2026"
            client.close()

    def test_init_sets_auth_header(self) -> None:
        with patch("httpx.Client") as mock_cls:
            mock_cls.return_value = MagicMock()
            client = NexusServiceClient("http://localhost:2026", "my-api-key")
            call_kwargs = mock_cls.call_args[1]
            assert call_kwargs["headers"]["Authorization"] == "Bearer my-api-key"
            client.close()

    def test_init_no_auth_header_when_no_key(self) -> None:
        with patch("httpx.Client") as mock_cls:
            mock_cls.return_value = MagicMock()
            client = NexusServiceClient("http://localhost:2026")
            call_kwargs = mock_cls.call_args[1]
            assert "Authorization" not in call_kwargs["headers"]
            client.close()

    def test_init_sets_content_type_header(self) -> None:
        with patch("httpx.Client") as mock_cls:
            mock_cls.return_value = MagicMock()
            client = NexusServiceClient("http://localhost:2026")
            call_kwargs = mock_cls.call_args[1]
            assert call_kwargs["headers"]["Content-Type"] == "application/json"
            client.close()

    def test_init_sets_timeout(self) -> None:
        with patch("httpx.Client") as mock_cls:
            mock_cls.return_value = MagicMock()
            client = NexusServiceClient("http://localhost:2026")
            call_kwargs = mock_cls.call_args[1]
            assert call_kwargs["timeout"] == 30.0
            client.close()


# ── TestContextManager ────────────────────────────────────


class TestContextManager:
    """Test __enter__ / __exit__ lifecycle."""

    def test_context_manager_closes_client(self) -> None:
        mock_http = MagicMock()
        with (
            patch("httpx.Client", return_value=mock_http),
            NexusServiceClient("http://localhost:2026") as client,
        ):
            assert client is not None
        mock_http.close.assert_called_once()

    def test_context_manager_returns_self(self) -> None:
        with patch("httpx.Client", return_value=MagicMock()):
            svc = NexusServiceClient("http://localhost:2026")
            with svc as ctx:
                assert ctx is svc
            svc.close()


# ── TestRequest ───────────────────────────────────────────


class TestRequest:
    """Test the internal _request method behavior."""

    def test_request_filters_none_params(
        self, client_and_mock: tuple[NexusServiceClient, MagicMock]
    ) -> None:
        client, mock_http = client_and_mock
        mock_http.request.return_value = _make_response(200, {"ok": True})

        client._request("GET", "/test", params={"keep": "yes", "drop": None})

        _, call_kwargs = mock_http.request.call_args
        assert call_kwargs["params"] == {"keep": "yes"}

    def test_request_raises_on_4xx(
        self, client_and_mock: tuple[NexusServiceClient, MagicMock]
    ) -> None:
        client, mock_http = client_and_mock
        mock_http.request.return_value = _make_response(404, {"detail": "Not found"})

        with pytest.raises(NexusAPIError) as exc_info:
            client._request("GET", "/missing")

        assert exc_info.value.status_code == 404
        assert "Not found" in exc_info.value.detail

    def test_request_raises_on_5xx(
        self, client_and_mock: tuple[NexusServiceClient, MagicMock]
    ) -> None:
        client, mock_http = client_and_mock
        mock_http.request.return_value = _make_response(500, {"detail": "Server error"})

        with pytest.raises(NexusAPIError) as exc_info:
            client._request("POST", "/broken")

        assert exc_info.value.status_code == 500
        assert "Server error" in exc_info.value.detail

    def test_request_raises_on_error_with_unparseable_json(
        self, client_and_mock: tuple[NexusServiceClient, MagicMock]
    ) -> None:
        client, mock_http = client_and_mock
        resp = MagicMock()
        resp.status_code = 502
        resp.text = "Bad Gateway"
        resp.json.side_effect = ValueError("No JSON")
        mock_http.request.return_value = resp

        with pytest.raises(NexusAPIError) as exc_info:
            client._request("GET", "/bad-gateway")

        assert exc_info.value.status_code == 502
        assert "Bad Gateway" in exc_info.value.detail

    def test_request_returns_empty_dict_on_204(
        self, client_and_mock: tuple[NexusServiceClient, MagicMock]
    ) -> None:
        client, mock_http = client_and_mock
        mock_http.request.return_value = _make_response(204)

        result = client._request("DELETE", "/resource")

        assert result == {}

    def test_request_returns_json(
        self, client_and_mock: tuple[NexusServiceClient, MagicMock]
    ) -> None:
        client, mock_http = client_and_mock
        expected = {"id": "abc", "status": "ok"}
        mock_http.request.return_value = _make_response(200, expected)

        result = client._request("GET", "/resource")

        assert result == expected

    def test_request_passes_json_body(
        self, client_and_mock: tuple[NexusServiceClient, MagicMock]
    ) -> None:
        client, mock_http = client_and_mock
        mock_http.request.return_value = _make_response(200, {"created": True})

        body = {"name": "test"}
        client._request("POST", "/resource", json_body=body)

        _, call_kwargs = mock_http.request.call_args
        assert call_kwargs["json"] == body


# ── TestPayMethods ────────────────────────────────────────


class TestPayMethods:
    """Test pay_balance, pay_transfer, pay_history."""

    def test_pay_balance_no_agent(
        self, client_and_mock: tuple[NexusServiceClient, MagicMock]
    ) -> None:
        client, mock_http = client_and_mock
        mock_http.request.return_value = _make_response(
            200, {"available": "100", "reserved": "0", "total": "100"}
        )

        result = client.pay_balance()

        mock_http.request.assert_called_once_with(
            "GET", "/api/v2/pay/balance", params=None, json=None
        )
        assert result == {"available": "100", "reserved": "0", "total": "100"}

    def test_pay_balance_with_agent(
        self, client_and_mock: tuple[NexusServiceClient, MagicMock]
    ) -> None:
        client, mock_http = client_and_mock
        mock_http.request.return_value = _make_response(
            200, {"available": "50", "reserved": "10", "total": "60"}
        )

        result = client.pay_balance(agent_id="agent-42")

        mock_http.request.assert_called_once_with(
            "GET", "/api/v2/pay/balance", params={"agent_id": "agent-42"}, json=None
        )
        assert result["available"] == "50"

    def test_pay_transfer(self, client_and_mock: tuple[NexusServiceClient, MagicMock]) -> None:
        client, mock_http = client_and_mock
        mock_http.request.return_value = _make_response(
            200, {"txn_id": "tx-1", "status": "completed"}
        )

        result = client.pay_transfer("bob", "10.00", memo="lunch", method="credits")

        mock_http.request.assert_called_once_with(
            "POST",
            "/api/v2/pay/transfer",
            params=None,
            json={"to": "bob", "amount": "10.00", "memo": "lunch", "method": "credits"},
        )
        assert result["txn_id"] == "tx-1"

    def test_pay_transfer_defaults(
        self, client_and_mock: tuple[NexusServiceClient, MagicMock]
    ) -> None:
        client, mock_http = client_and_mock
        mock_http.request.return_value = _make_response(200, {"txn_id": "tx-2"})

        client.pay_transfer("alice", "5.00")

        _, call_kwargs = mock_http.request.call_args
        assert call_kwargs["json"]["memo"] == ""
        assert call_kwargs["json"]["method"] == "auto"

    def test_pay_history(self, client_and_mock: tuple[NexusServiceClient, MagicMock]) -> None:
        client, mock_http = client_and_mock
        mock_http.request.return_value = _make_response(200, {"items": [], "cursor": None})

        result = client.pay_history(since="2025-01-01", limit=10)

        _, call_kwargs = mock_http.request.call_args
        # None values are filtered by _request
        assert call_kwargs["params"]["since"] == "2025-01-01"
        assert call_kwargs["params"]["limit"] == 10
        assert result["items"] == []


# ── TestAuditMethods ──────────────────────────────────────


class TestAuditMethods:
    """Test audit_list and audit_export."""

    def test_audit_list_default_params(
        self, client_and_mock: tuple[NexusServiceClient, MagicMock]
    ) -> None:
        client, mock_http = client_and_mock
        mock_http.request.return_value = _make_response(200, {"items": [], "total": 0})

        result = client.audit_list()

        mock_http.request.assert_called_once()
        _, call_kwargs = mock_http.request.call_args
        assert call_kwargs["params"]["limit"] == 50
        assert result["total"] == 0

    def test_audit_list_with_filters(
        self, client_and_mock: tuple[NexusServiceClient, MagicMock]
    ) -> None:
        client, mock_http = client_and_mock
        mock_http.request.return_value = _make_response(200, {"items": [{"id": "1"}]})

        result = client.audit_list(
            since="2025-01-01",
            until="2025-12-31",
            agent_id="agent-1",
            action="transfer",
            limit=10,
            cursor="abc",
        )

        _, call_kwargs = mock_http.request.call_args
        assert call_kwargs["params"]["since"] == "2025-01-01"
        assert call_kwargs["params"]["until"] == "2025-12-31"
        assert call_kwargs["params"]["agent_id"] == "agent-1"
        assert call_kwargs["params"]["status"] == "transfer"
        assert call_kwargs["params"]["limit"] == 10
        assert call_kwargs["params"]["cursor"] == "abc"
        assert len(result["items"]) == 1

    def test_audit_export(self, client_and_mock: tuple[NexusServiceClient, MagicMock]) -> None:
        client, mock_http = client_and_mock
        resp = _make_response(200, text="col1,col2\nval1,val2")
        resp.text = "col1,col2\nval1,val2"
        mock_http.request.return_value = resp

        result = client.audit_export(fmt="csv", since="2025-01-01")

        mock_http.request.assert_called_once()
        call_args = mock_http.request.call_args
        assert call_args[0] == ("GET", "/api/v2/audit/transactions/export")
        assert call_args[1]["params"]["format"] == "csv"
        assert call_args[1]["params"]["since"] == "2025-01-01"
        assert result == "col1,col2\nval1,val2"

    def test_audit_export_error_raises(
        self, client_and_mock: tuple[NexusServiceClient, MagicMock]
    ) -> None:
        client, mock_http = client_and_mock
        mock_http.request.return_value = _make_response(403, {"detail": "Forbidden"})

        with pytest.raises(NexusAPIError) as exc_info:
            client.audit_export()

        assert exc_info.value.status_code == 403


# ── TestLockMethods ───────────────────────────────────────


class TestLockMethods:
    """Test lock_list, lock_info, lock_release."""

    def test_lock_list(self, client_and_mock: tuple[NexusServiceClient, MagicMock]) -> None:
        client, mock_http = client_and_mock
        mock_http.request.return_value = _make_response(200, {"locks": []})

        result = client.lock_list(zone_id="zone-a")

        _, call_kwargs = mock_http.request.call_args
        assert call_kwargs["params"]["zone_id"] == "zone-a"
        assert result == {"locks": []}

    def test_lock_info_strips_slash(
        self, client_and_mock: tuple[NexusServiceClient, MagicMock]
    ) -> None:
        client, mock_http = client_and_mock
        mock_http.request.return_value = _make_response(
            200, {"path": "data/file.txt", "locked": True}
        )

        result = client.lock_info("/data/file.txt")

        call_args = mock_http.request.call_args
        # The leading slash should be stripped from the path segment
        assert call_args[0] == ("GET", "/api/v2/locks/data/file.txt")
        assert result["locked"] is True

    def test_lock_release_force(
        self, client_and_mock: tuple[NexusServiceClient, MagicMock]
    ) -> None:
        client, mock_http = client_and_mock
        mock_http.request.return_value = _make_response(204)

        result = client.lock_release("/data/file.txt", lock_id="lk-1", force=True)

        call_args = mock_http.request.call_args
        assert call_args[0] == ("DELETE", "/api/v2/locks/data/file.txt")
        assert call_args[1]["params"]["lock_id"] == "lk-1"
        assert call_args[1]["params"]["force"] == "true"
        assert result == {}

    def test_lock_release_no_force(
        self, client_and_mock: tuple[NexusServiceClient, MagicMock]
    ) -> None:
        client, mock_http = client_and_mock
        mock_http.request.return_value = _make_response(204)

        client.lock_release("data/file.txt")

        _, call_kwargs = mock_http.request.call_args
        assert "force" not in call_kwargs["params"]
        assert "lock_id" not in call_kwargs["params"]


# ── TestGovernanceMethods ─────────────────────────────────


class TestGovernanceMethods:
    """Test governance_alerts, governance_status, governance_rings."""

    def test_governance_alerts(self, client_and_mock: tuple[NexusServiceClient, MagicMock]) -> None:
        client, mock_http = client_and_mock
        mock_http.request.return_value = _make_response(200, {"alerts": [{"id": "a1"}]})

        result = client.governance_alerts(severity="high", since="2025-01-01", limit=10)

        _, call_kwargs = mock_http.request.call_args
        assert call_kwargs["params"]["severity"] == "high"
        assert call_kwargs["params"]["since"] == "2025-01-01"
        assert call_kwargs["params"]["limit"] == 10
        assert len(result["alerts"]) == 1

    def test_governance_status_combines_alerts_and_rings(
        self, client_and_mock: tuple[NexusServiceClient, MagicMock]
    ) -> None:
        client, mock_http = client_and_mock
        alerts_resp = _make_response(200, {"alerts": [{"id": "a1"}]})
        rings_resp = _make_response(200, {"rings": []})
        mock_http.request.side_effect = [alerts_resp, rings_resp]

        result = client.governance_status()

        assert mock_http.request.call_count == 2
        assert result["recent_alerts"] == {"alerts": [{"id": "a1"}]}
        assert result["fraud_rings"] == {"rings": []}

    def test_governance_rings(self, client_and_mock: tuple[NexusServiceClient, MagicMock]) -> None:
        client, mock_http = client_and_mock
        mock_http.request.return_value = _make_response(200, {"rings": [{"id": "r1"}]})

        result = client.governance_rings()

        call_args = mock_http.request.call_args
        assert call_args[0] == ("GET", "/api/v2/governance/rings")
        assert result["rings"] == [{"id": "r1"}]


# ── TestEventMethods ──────────────────────────────────────


class TestEventMethods:
    """Test events_replay and events_subscribe."""

    def test_events_replay(self, client_and_mock: tuple[NexusServiceClient, MagicMock]) -> None:
        client, mock_http = client_and_mock
        mock_http.request.return_value = _make_response(200, {"events": [{"type": "write"}]})

        result = client.events_replay(
            since="2025-01-01", event_type="write", path="/data", limit=25
        )

        _, call_kwargs = mock_http.request.call_args
        assert call_kwargs["params"]["event_type"] == "write"
        assert call_kwargs["params"]["path"] == "/data"
        assert call_kwargs["params"]["limit"] == 25
        assert len(result["events"]) == 1

    def test_events_replay_defaults(
        self, client_and_mock: tuple[NexusServiceClient, MagicMock]
    ) -> None:
        client, mock_http = client_and_mock
        mock_http.request.return_value = _make_response(200, {"events": []})

        client.events_replay()

        _, call_kwargs = mock_http.request.call_args
        assert call_kwargs["params"]["limit"] == 50

    def test_events_subscribe(self, client_and_mock: tuple[NexusServiceClient, MagicMock]) -> None:
        client, mock_http = client_and_mock
        resp = MagicMock()
        resp.status_code = 200
        resp.text = 'data: {"type": "write"}\n\n'
        mock_http.request.return_value = resp

        result = client.events_subscribe("/**")

        call_args = mock_http.request.call_args
        assert call_args[0] == ("GET", "/api/v2/events/stream")
        assert call_args[1]["params"] == {"pattern": "/**"}
        assert call_args[1]["headers"]["Accept"] == "text/event-stream"
        assert result == resp.text

    def test_events_subscribe_error_raises(
        self, client_and_mock: tuple[NexusServiceClient, MagicMock]
    ) -> None:
        client, mock_http = client_and_mock
        mock_http.request.return_value = _make_response(401, {"detail": "Unauthorized"})

        with pytest.raises(NexusAPIError) as exc_info:
            client.events_subscribe("/**")

        assert exc_info.value.status_code == 401


# ── TestSnapshotMethods ───────────────────────────────────


class TestSnapshotMethods:
    """Test snapshot_create, snapshot_list, snapshot_restore."""

    def test_snapshot_create(self, client_and_mock: tuple[NexusServiceClient, MagicMock]) -> None:
        client, mock_http = client_and_mock
        mock_http.request.return_value = _make_response(200, {"txn_id": "snap-1"})

        result = client.snapshot_create(description="backup", ttl_seconds=7200)

        _, call_kwargs = mock_http.request.call_args
        assert call_kwargs["json"] == {"ttl_seconds": 7200, "description": "backup"}
        assert result["txn_id"] == "snap-1"

    def test_snapshot_create_no_description(
        self, client_and_mock: tuple[NexusServiceClient, MagicMock]
    ) -> None:
        client, mock_http = client_and_mock
        mock_http.request.return_value = _make_response(200, {"txn_id": "snap-2"})

        client.snapshot_create()

        _, call_kwargs = mock_http.request.call_args
        # description should NOT be in the body when omitted
        assert "description" not in call_kwargs["json"]
        assert call_kwargs["json"]["ttl_seconds"] == 3600

    def test_snapshot_list(self, client_and_mock: tuple[NexusServiceClient, MagicMock]) -> None:
        client, mock_http = client_and_mock
        mock_http.request.return_value = _make_response(200, {"snapshots": [{"id": "s1"}]})

        result = client.snapshot_list()

        call_args = mock_http.request.call_args
        assert call_args[0] == ("GET", "/api/v2/snapshots")
        assert len(result["snapshots"]) == 1

    def test_snapshot_restore(self, client_and_mock: tuple[NexusServiceClient, MagicMock]) -> None:
        client, mock_http = client_and_mock
        mock_http.request.return_value = _make_response(200, {"status": "restored"})

        result = client.snapshot_restore("txn-abc")

        call_args = mock_http.request.call_args
        assert call_args[0] == ("POST", "/api/v2/snapshots/txn-abc/rollback")
        assert result["status"] == "restored"


# ── TestNexusAPIError ─────────────────────────────────────


class TestNexusAPIError:
    """Test the NexusAPIError exception."""

    def test_stores_status_code_and_detail(self) -> None:
        err = NexusAPIError(422, "Validation failed")
        assert err.status_code == 422
        assert err.detail == "Validation failed"

    def test_str_includes_status_and_detail(self) -> None:
        err = NexusAPIError(500, "Internal server error")
        assert "500" in str(err)
        assert "Internal server error" in str(err)
