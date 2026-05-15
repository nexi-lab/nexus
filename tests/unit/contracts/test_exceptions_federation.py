from nexus.contracts.exceptions import (
    HandshakeAuthError,
    HandshakeConnectionError,
    NexusError,
    ZoneReadOnlyError,
    ZoneUnavailableError,
)


def test_zone_read_only_is_nexus_error() -> None:
    exc = ZoneReadOnlyError("Zone 'company' is read-only")
    assert isinstance(exc, NexusError)
    assert "company" in str(exc)


def test_zone_unavailable_is_nexus_error() -> None:
    exc = ZoneUnavailableError("Zone 'company' is unavailable")
    assert isinstance(exc, NexusError)


def test_handshake_auth_error_is_nexus_error() -> None:
    exc = HandshakeAuthError("Token rejected by hub")
    assert isinstance(exc, NexusError)


def test_handshake_connection_error_is_nexus_error() -> None:
    exc = HandshakeConnectionError("Hub unreachable")
    assert isinstance(exc, NexusError)


def test_zone_read_only_has_correct_attributes() -> None:
    exc = ZoneReadOnlyError("Zone 'company' is read-only")
    assert exc.is_expected is True
    assert exc.status_code == 403


def test_handshake_auth_error_has_correct_attributes() -> None:
    exc = HandshakeAuthError("Token rejected")
    assert exc.is_expected is True
    assert exc.status_code == 401
