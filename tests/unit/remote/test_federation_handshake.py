"""Tests for FederationHandshake (issue #3786)."""

from unittest.mock import MagicMock, patch

import pytest

from nexus.contracts.exceptions import (
    AuthenticationError,
    HandshakeAuthError,
    HandshakeConnectionError,
)
from nexus.remote.federation_handshake import FederationHandshake, HubSession, HubZoneGrant


def test_successful_handshake_returns_hub_session():
    """Success path: hub returns two zone grants."""
    mock_transport = MagicMock()
    mock_transport.call_rpc.return_value = {
        "zones": [
            {"zone_id": "company", "permission": "r"},
            {"zone_id": "shared", "permission": "rw"},
        ]
    }
    with patch("nexus.remote.federation_handshake.RPCTransport", return_value=mock_transport):
        handshake = FederationHandshake(hub_url="grpc://hub.example.com", token="tok-abc")
        session = handshake.run()

    assert isinstance(session, HubSession)
    assert session.transport is mock_transport
    assert len(session.zones) == 2
    assert session.zones[0] == HubZoneGrant(zone_id="company", permission="r")
    assert session.zones[1] == HubZoneGrant(zone_id="shared", permission="rw")


def test_handshake_401_raises_auth_error():
    """Hub rejects the token with 401 → HandshakeAuthError."""
    mock_transport = MagicMock()
    mock_transport.call_rpc.side_effect = AuthenticationError("Unauthorized")
    with patch("nexus.remote.federation_handshake.RPCTransport", return_value=mock_transport):
        handshake = FederationHandshake(hub_url="grpc://hub.example.com", token="bad-token")
        with pytest.raises(HandshakeAuthError):
            handshake.run()


def test_handshake_unreachable_raises_connection_error():
    """Hub is unreachable → HandshakeConnectionError."""
    mock_transport = MagicMock()
    mock_transport.call_rpc.side_effect = OSError("Connection refused")
    with patch("nexus.remote.federation_handshake.RPCTransport", return_value=mock_transport):
        handshake = FederationHandshake(hub_url="grpc://hub.example.com", token="tok-abc")
        with pytest.raises(HandshakeConnectionError):
            handshake.run()


def test_handshake_bad_url_raises_connection_error():
    """RPCTransport raises ValueError for bad URL → HandshakeConnectionError."""
    with patch(
        "nexus.remote.federation_handshake.RPCTransport",
        side_effect=ValueError("Insecure gRPC channel refused"),
    ):
        handshake = FederationHandshake(hub_url="grpc://bad-url", token="tok")
        with pytest.raises(HandshakeConnectionError):
            handshake.run()
