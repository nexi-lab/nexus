"""Tests for NexusServiceClient HTTP REST client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nexus.cli.client import NexusAPIError, NexusServiceClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_httpx_client() -> MagicMock:
    """Pre-configured mock for httpx.Client."""
    client = MagicMock()
    # Default successful response
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"ok": True}
    response.text = '{"ok": true}'
    client.request.return_value = response
    return client


@pytest.fixture()
def service_client(mock_httpx_client: MagicMock) -> NexusServiceClient:
    """NexusServiceClient with an injected mock httpx client."""
    with patch("httpx.Client", return_value=mock_httpx_client):
        sc = NexusServiceClient("http://localhost:2026", api_key="test-key")
    return sc


# ---------------------------------------------------------------------------
# TestNexusServiceClient
# ---------------------------------------------------------------------------


class TestNexusServiceClient:
    """Tests for NexusServiceClient."""

    def test_context_manager(self) -> None:
        """__enter__ returns self; __exit__ closes the client."""
        mock_client_instance = MagicMock()
        with patch("httpx.Client", return_value=mock_client_instance):
            sc = NexusServiceClient("http://localhost:2026", api_key="k")

        # __enter__
        result = sc.__enter__()
        assert result is sc

        # __exit__
        sc.__exit__(None, None, None)
        mock_client_instance.close.assert_called_once()

    def test_request_success(
        self,
        service_client: NexusServiceClient,
        mock_httpx_client: MagicMock,
    ) -> None:
        """Successful request returns parsed JSON."""
        mock_httpx_client.request.return_value.json.return_value = {"balance": "100"}
        mock_httpx_client.request.return_value.status_code = 200

        result = service_client._request("GET", "/api/v2/pay/balance")

        assert result == {"balance": "100"}
        mock_httpx_client.request.assert_called_once_with(
            "GET",
            "/api/v2/pay/balance",
            params=None,
            json=None,
        )

    def test_request_error_raises(
        self,
        service_client: NexusServiceClient,
        mock_httpx_client: MagicMock,
    ) -> None:
        """HTTP 400+ raises NexusAPIError with status code and detail."""
        err_response = MagicMock()
        err_response.status_code = 422
        err_response.text = "Validation failed"
        err_response.json.return_value = {"detail": "bad input"}
        mock_httpx_client.request.return_value = err_response

        with pytest.raises(NexusAPIError) as exc_info:
            service_client._request("POST", "/api/v2/pay/transfer")

        assert exc_info.value.status_code == 422
        assert exc_info.value.detail == "bad input"

    def test_request_filters_none_params(
        self,
        service_client: NexusServiceClient,
        mock_httpx_client: MagicMock,
    ) -> None:
        """None values in query params are removed before sending."""
        service_client._request(
            "GET",
            "/api/v2/audit/transactions",
            params={"since": None, "limit": 50, "until": None},
        )

        call_kwargs = mock_httpx_client.request.call_args
        assert call_kwargs.kwargs["params"] == {"limit": 50}

    def test_request_204_returns_empty_dict(
        self,
        service_client: NexusServiceClient,
        mock_httpx_client: MagicMock,
    ) -> None:
        """HTTP 204 No Content returns empty dict."""
        no_content = MagicMock()
        no_content.status_code = 204
        mock_httpx_client.request.return_value = no_content

        result = service_client._request("DELETE", "/api/v2/locks/foo")

        assert result == {}

    def test_pay_balance(
        self,
        service_client: NexusServiceClient,
        mock_httpx_client: MagicMock,
    ) -> None:
        """pay_balance calls GET /api/v2/pay/balance."""
        service_client.pay_balance(agent_id="agent-1")

        mock_httpx_client.request.assert_called_once_with(
            "GET",
            "/api/v2/pay/balance",
            params={"agent_id": "agent-1"},
            json=None,
        )

    def test_pay_transfer(
        self,
        service_client: NexusServiceClient,
        mock_httpx_client: MagicMock,
    ) -> None:
        """pay_transfer POSTs with correct JSON body."""
        service_client.pay_transfer(to="agent-2", amount="50", memo="tip")

        mock_httpx_client.request.assert_called_once_with(
            "POST",
            "/api/v2/pay/transfer",
            params=None,
            json={"to": "agent-2", "amount": "50", "memo": "tip", "method": "auto"},
        )

    def test_audit_list(
        self,
        service_client: NexusServiceClient,
        mock_httpx_client: MagicMock,
    ) -> None:
        """audit_list calls GET /api/v2/audit/transactions with mapped params."""
        service_client.audit_list(agent_id="a1", action="completed", limit=10)

        call_kwargs = mock_httpx_client.request.call_args
        assert call_kwargs.args == ("GET", "/api/v2/audit/transactions")
        sent_params = call_kwargs.kwargs["params"]
        assert sent_params["agent_id"] == "a1"
        assert sent_params["status"] == "completed"
        assert sent_params["limit"] == 10

    def test_lock_release(
        self,
        service_client: NexusServiceClient,
        mock_httpx_client: MagicMock,
    ) -> None:
        """lock_release sends DELETE with correct path and params."""
        service_client.lock_release(path="/data/file.txt", lock_id="lk1", force=True)

        call_kwargs = mock_httpx_client.request.call_args
        assert call_kwargs.args == ("DELETE", "/api/v2/locks/data/file.txt")
        sent_params = call_kwargs.kwargs["params"]
        assert sent_params["force"] == "true"
        assert sent_params["lock_id"] == "lk1"

    def test_governance_status(
        self,
        service_client: NexusServiceClient,
        mock_httpx_client: MagicMock,
    ) -> None:
        """governance_status fetches alerts + rings and combines them."""
        mock_httpx_client.request.return_value.json.return_value = {"items": []}
        mock_httpx_client.request.return_value.status_code = 200

        result = service_client.governance_status()

        assert mock_httpx_client.request.call_count == 2
        assert "recent_alerts" in result
        assert "fraud_rings" in result
