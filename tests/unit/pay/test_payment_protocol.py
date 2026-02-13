"""Tests for Protocol Abstraction Layer.

TDD tests for Issue #1357 Phase 1: PaymentProtocol ABC, ProtocolDetector,
ProtocolRegistry, and concrete protocol implementations (X402, Credits).

Test categories:
1. ABC & data classes (cannot instantiate, frozen dataclasses)
2. Exception hierarchy
3. ProtocolDetector (detect, ordering, no-match error)
4. ProtocolRegistry (register, get, unregister, resolve, legacy mapping)
5. X402PaymentProtocol (can_handle, transfer delegation)
6. CreditsPaymentProtocol (can_handle, transfer delegation)
7. Error propagation
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from nexus.pay.audit_types import TransactionProtocol

# =============================================================================
# 1. ABC & Data Classes
# =============================================================================


class TestPaymentProtocolABC:
    """PaymentProtocol ABC cannot be instantiated directly."""

    def test_abc_cannot_be_instantiated(self):
        from nexus.pay.protocol import PaymentProtocol

        with pytest.raises(TypeError):
            PaymentProtocol()  # type: ignore[abstract]

    def test_abc_requires_protocol_name(self):
        """Subclass without protocol_name should fail."""
        from nexus.pay.protocol import PaymentProtocol

        class Incomplete(PaymentProtocol):
            def can_handle(self, to: str, metadata: dict | None = None) -> bool:
                return True

            async def transfer(self, request):
                pass

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_abc_requires_can_handle(self):
        """Subclass without can_handle should fail."""
        from nexus.pay.protocol import PaymentProtocol

        class Incomplete(PaymentProtocol):
            @property
            def protocol_name(self):
                return TransactionProtocol.INTERNAL

            async def transfer(self, request):
                pass

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_abc_requires_transfer(self):
        """Subclass without transfer should fail."""
        from nexus.pay.protocol import PaymentProtocol

        class Incomplete(PaymentProtocol):
            @property
            def protocol_name(self):
                return TransactionProtocol.INTERNAL

            def can_handle(self, to: str, metadata: dict | None = None) -> bool:
                return True

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]


class TestProtocolTransferRequest:
    """ProtocolTransferRequest is a frozen immutable dataclass."""

    def test_create_request(self):
        from nexus.pay.protocol import ProtocolTransferRequest

        req = ProtocolTransferRequest(
            from_agent="alice",
            to="bob",
            amount=Decimal("1.50"),
            memo="test",
        )
        assert req.from_agent == "alice"
        assert req.to == "bob"
        assert req.amount == Decimal("1.50")
        assert req.memo == "test"
        assert req.idempotency_key is None
        assert req.metadata == {}

    def test_request_is_frozen(self):
        from nexus.pay.protocol import ProtocolTransferRequest

        req = ProtocolTransferRequest(
            from_agent="alice",
            to="bob",
            amount=Decimal("1.0"),
        )
        with pytest.raises(AttributeError):
            req.amount = Decimal("2.0")  # type: ignore[misc]

    def test_request_with_metadata(self):
        from nexus.pay.protocol import ProtocolTransferRequest

        req = ProtocolTransferRequest(
            from_agent="alice",
            to="bob",
            amount=Decimal("1.0"),
            metadata={"tx_type": "api_call"},
        )
        assert req.metadata == {"tx_type": "api_call"}


class TestProtocolTransferResult:
    """ProtocolTransferResult is a frozen immutable dataclass."""

    def test_create_result(self):
        from nexus.pay.protocol import ProtocolTransferResult

        result = ProtocolTransferResult(
            protocol=TransactionProtocol.INTERNAL,
            tx_id="tx-123",
            amount=Decimal("5.0"),
            from_agent="alice",
            to="bob",
        )
        assert result.protocol == TransactionProtocol.INTERNAL
        assert result.tx_id == "tx-123"
        assert result.tx_hash is None
        assert result.timestamp is None
        assert result.metadata == {}

    def test_result_is_frozen(self):
        from nexus.pay.protocol import ProtocolTransferResult

        result = ProtocolTransferResult(
            protocol=TransactionProtocol.X402,
            tx_id="tx-456",
            amount=Decimal("1.0"),
            from_agent="alice",
            to="bob",
        )
        with pytest.raises(AttributeError):
            result.tx_id = "changed"  # type: ignore[misc]

    def test_result_with_all_fields(self):
        from datetime import UTC, datetime

        from nexus.pay.protocol import ProtocolTransferResult

        now = datetime.now(UTC)
        result = ProtocolTransferResult(
            protocol=TransactionProtocol.X402,
            tx_id="tx-789",
            amount=Decimal("2.0"),
            from_agent="alice",
            to="0xwallet",
            tx_hash="0xhash",
            timestamp=now,
            metadata={"network": "base"},
        )
        assert result.tx_hash == "0xhash"
        assert result.timestamp == now
        assert result.metadata == {"network": "base"}


# =============================================================================
# 2. Exception Hierarchy
# =============================================================================


class TestExceptionHierarchy:
    """Exception classes form a proper hierarchy."""

    def test_protocol_error_is_exception(self):
        from nexus.pay.protocol import ProtocolError

        assert issubclass(ProtocolError, Exception)

    def test_not_found_is_protocol_error(self):
        from nexus.pay.protocol import ProtocolError, ProtocolNotFoundError

        assert issubclass(ProtocolNotFoundError, ProtocolError)

    def test_detection_error_is_protocol_error(self):
        from nexus.pay.protocol import ProtocolDetectionError, ProtocolError

        assert issubclass(ProtocolDetectionError, ProtocolError)

    def test_not_found_error_message(self):
        from nexus.pay.protocol import ProtocolNotFoundError

        err = ProtocolNotFoundError("unknown_proto")
        assert "unknown_proto" in str(err)

    def test_detection_error_message(self):
        from nexus.pay.protocol import ProtocolDetectionError

        err = ProtocolDetectionError("no match")
        assert "no match" in str(err)


# =============================================================================
# 3. ProtocolDetector
# =============================================================================


class TestProtocolDetector:
    """ProtocolDetector detects the right protocol from an ordered chain."""

    def _make_stub_protocol(self, name: TransactionProtocol, handles: bool):
        """Create a stub protocol that always returns `handles` from can_handle."""
        from nexus.pay.protocol import PaymentProtocol

        class Stub(PaymentProtocol):
            @property
            def protocol_name(self) -> TransactionProtocol:
                return name

            def can_handle(self, to: str, metadata: dict | None = None) -> bool:
                return handles

            async def transfer(self, request):
                pass

        return Stub()

    def test_detect_returns_first_match(self):
        from nexus.pay.protocol import ProtocolDetector

        p1 = self._make_stub_protocol(TransactionProtocol.X402, handles=True)
        p2 = self._make_stub_protocol(TransactionProtocol.INTERNAL, handles=True)
        detector = ProtocolDetector([p1, p2])

        result = detector.detect("anything")
        assert result.protocol_name == TransactionProtocol.X402

    def test_detect_skips_non_matching(self):
        from nexus.pay.protocol import ProtocolDetector

        p1 = self._make_stub_protocol(TransactionProtocol.X402, handles=False)
        p2 = self._make_stub_protocol(TransactionProtocol.INTERNAL, handles=True)
        detector = ProtocolDetector([p1, p2])

        result = detector.detect("anything")
        assert result.protocol_name == TransactionProtocol.INTERNAL

    def test_detect_raises_on_no_match(self):
        from nexus.pay.protocol import ProtocolDetectionError, ProtocolDetector

        p1 = self._make_stub_protocol(TransactionProtocol.X402, handles=False)
        detector = ProtocolDetector([p1])

        with pytest.raises(ProtocolDetectionError):
            detector.detect("anything")

    def test_detect_empty_chain_raises(self):
        from nexus.pay.protocol import ProtocolDetectionError, ProtocolDetector

        detector = ProtocolDetector([])
        with pytest.raises(ProtocolDetectionError):
            detector.detect("anything")

    def test_detect_passes_metadata(self):
        """Metadata is forwarded to can_handle."""
        from nexus.pay.protocol import PaymentProtocol, ProtocolDetector

        received_metadata = {}

        class MetadataCapture(PaymentProtocol):
            @property
            def protocol_name(self) -> TransactionProtocol:
                return TransactionProtocol.ACP

            def can_handle(self, to: str, metadata: dict | None = None) -> bool:
                received_metadata.update(metadata or {})
                return True

            async def transfer(self, request):
                pass

        detector = ProtocolDetector([MetadataCapture()])
        detector.detect("target", metadata={"protocol": "acp"})
        assert received_metadata == {"protocol": "acp"}


# =============================================================================
# 4. ProtocolRegistry
# =============================================================================


class TestProtocolRegistry:
    """ProtocolRegistry manages protocol registration and lookup."""

    def _make_stub_protocol(self, name: TransactionProtocol, handles: bool):
        from nexus.pay.protocol import PaymentProtocol

        class Stub(PaymentProtocol):
            @property
            def protocol_name(self) -> TransactionProtocol:
                return name

            def can_handle(self, to: str, metadata: dict | None = None) -> bool:
                return handles

            async def transfer(self, request):
                pass

        return Stub()

    def test_register_and_get(self):
        from nexus.pay.protocol import ProtocolRegistry

        registry = ProtocolRegistry()
        proto = self._make_stub_protocol(TransactionProtocol.X402, handles=True)
        registry.register(proto)

        assert registry.get("x402") is proto

    def test_get_unknown_raises(self):
        from nexus.pay.protocol import ProtocolNotFoundError, ProtocolRegistry

        registry = ProtocolRegistry()
        with pytest.raises(ProtocolNotFoundError):
            registry.get("nonexistent")

    def test_unregister(self):
        from nexus.pay.protocol import ProtocolNotFoundError, ProtocolRegistry

        registry = ProtocolRegistry()
        proto = self._make_stub_protocol(TransactionProtocol.X402, handles=True)
        registry.register(proto)
        registry.unregister("x402")

        with pytest.raises(ProtocolNotFoundError):
            registry.get("x402")

    def test_unregister_unknown_is_noop(self):
        from nexus.pay.protocol import ProtocolRegistry

        registry = ProtocolRegistry()
        registry.unregister("nonexistent")  # Should not raise

    def test_resolve_by_method_name(self):
        from nexus.pay.protocol import ProtocolRegistry

        registry = ProtocolRegistry()
        proto = self._make_stub_protocol(TransactionProtocol.X402, handles=True)
        registry.register(proto)

        resolved = registry.resolve(method="x402", to="anything")
        assert resolved is proto

    def test_resolve_auto_uses_detector(self):
        from nexus.pay.protocol import ProtocolRegistry

        registry = ProtocolRegistry()
        x402 = self._make_stub_protocol(TransactionProtocol.X402, handles=False)
        credits = self._make_stub_protocol(TransactionProtocol.INTERNAL, handles=True)
        registry.register(x402)
        registry.register(credits)

        resolved = registry.resolve(method="auto", to="agent-bob")
        assert resolved.protocol_name == TransactionProtocol.INTERNAL

    def test_resolve_legacy_credits_maps_to_internal(self):
        """method='credits' should resolve to the INTERNAL protocol."""
        from nexus.pay.protocol import ProtocolRegistry

        registry = ProtocolRegistry()
        proto = self._make_stub_protocol(TransactionProtocol.INTERNAL, handles=True)
        registry.register(proto)

        resolved = registry.resolve(method="credits", to="agent-bob")
        assert resolved.protocol_name == TransactionProtocol.INTERNAL

    def test_resolve_unknown_method_raises(self):
        from nexus.pay.protocol import ProtocolNotFoundError, ProtocolRegistry

        registry = ProtocolRegistry()
        with pytest.raises(ProtocolNotFoundError):
            registry.resolve(method="unknown", to="bob")

    def test_list_protocols(self):
        from nexus.pay.protocol import ProtocolRegistry

        registry = ProtocolRegistry()
        p1 = self._make_stub_protocol(TransactionProtocol.X402, handles=True)
        p2 = self._make_stub_protocol(TransactionProtocol.INTERNAL, handles=True)
        registry.register(p1)
        registry.register(p2)

        names = registry.list_protocols()
        assert set(names) == {"x402", "internal"}


# =============================================================================
# 5. X402PaymentProtocol
# =============================================================================


class TestX402PaymentProtocol:
    """X402PaymentProtocol wraps X402Client."""

    def test_protocol_name(self):
        from nexus.pay.protocol import X402PaymentProtocol

        mock_client = AsyncMock()
        proto = X402PaymentProtocol(client=mock_client)
        assert proto.protocol_name == TransactionProtocol.X402

    def test_can_handle_wallet_address(self):
        from nexus.pay.protocol import X402PaymentProtocol

        mock_client = AsyncMock()
        proto = X402PaymentProtocol(client=mock_client)
        assert proto.can_handle("0x1234567890abcdef1234567890abcdef12345678") is True

    def test_cannot_handle_agent_id(self):
        from nexus.pay.protocol import X402PaymentProtocol

        mock_client = AsyncMock()
        proto = X402PaymentProtocol(client=mock_client)
        assert proto.can_handle("agent-bob") is False
        assert proto.can_handle("my-service") is False

    @pytest.mark.asyncio
    async def test_transfer_delegates_to_client(self):
        from datetime import UTC, datetime

        from nexus.pay.protocol import ProtocolTransferRequest, X402PaymentProtocol
        from nexus.pay.x402 import X402Receipt

        now = datetime.now(UTC)
        mock_client = AsyncMock()
        mock_client.pay = AsyncMock(
            return_value=X402Receipt(
                tx_hash="0xdeadbeef",
                network="eip155:8453",
                amount=Decimal("1.0"),
                currency="USDC",
                timestamp=now,
            )
        )

        proto = X402PaymentProtocol(client=mock_client)
        request = ProtocolTransferRequest(
            from_agent="alice",
            to="0x1234567890abcdef1234567890abcdef12345678",
            amount=Decimal("1.0"),
            memo="pay",
        )

        result = await proto.transfer(request)
        assert result.protocol == TransactionProtocol.X402
        assert result.tx_id == "0xdeadbeef"
        assert result.tx_hash == "0xdeadbeef"
        assert result.amount == Decimal("1.0")
        assert result.timestamp == now
        mock_client.pay.assert_called_once_with(
            to_address="0x1234567890abcdef1234567890abcdef12345678",
            amount=Decimal("1.0"),
        )

    @pytest.mark.asyncio
    async def test_transfer_propagates_x402_error(self):
        from nexus.pay.protocol import ProtocolError, ProtocolTransferRequest, X402PaymentProtocol
        from nexus.pay.x402 import X402Error

        mock_client = AsyncMock()
        mock_client.pay = AsyncMock(side_effect=X402Error("Payment failed"))

        proto = X402PaymentProtocol(client=mock_client)
        request = ProtocolTransferRequest(
            from_agent="alice",
            to="0x1234567890abcdef1234567890abcdef12345678",
            amount=Decimal("1.0"),
        )

        with pytest.raises(ProtocolError, match="Payment failed"):
            await proto.transfer(request)


# =============================================================================
# 6. CreditsPaymentProtocol
# =============================================================================


class TestCreditsPaymentProtocol:
    """CreditsPaymentProtocol wraps CreditsService."""

    def test_protocol_name(self):
        from nexus.pay.protocol import CreditsPaymentProtocol

        mock_service = AsyncMock()
        proto = CreditsPaymentProtocol(service=mock_service, zone_id="default")
        assert proto.protocol_name == TransactionProtocol.INTERNAL

    def test_can_handle_agent_id(self):
        from nexus.pay.protocol import CreditsPaymentProtocol

        mock_service = AsyncMock()
        proto = CreditsPaymentProtocol(service=mock_service, zone_id="default")
        assert proto.can_handle("agent-bob") is True
        assert proto.can_handle("my-service") is True

    def test_cannot_handle_wallet_address(self):
        from nexus.pay.protocol import CreditsPaymentProtocol

        mock_service = AsyncMock()
        proto = CreditsPaymentProtocol(service=mock_service, zone_id="default")
        assert proto.can_handle("0x1234567890abcdef1234567890abcdef12345678") is False

    @pytest.mark.asyncio
    async def test_transfer_delegates_to_service(self):
        from nexus.pay.protocol import CreditsPaymentProtocol, ProtocolTransferRequest

        mock_service = AsyncMock()
        mock_service.transfer = AsyncMock(return_value="tx-abc")

        proto = CreditsPaymentProtocol(service=mock_service, zone_id="test-zone")
        request = ProtocolTransferRequest(
            from_agent="alice",
            to="bob",
            amount=Decimal("5.0"),
            memo="task payment",
            idempotency_key="key-123",
        )

        result = await proto.transfer(request)
        assert result.protocol == TransactionProtocol.INTERNAL
        assert result.tx_id == "tx-abc"
        assert result.amount == Decimal("5.0")
        assert result.tx_hash is None
        mock_service.transfer.assert_called_once_with(
            from_id="alice",
            to_id="bob",
            amount=Decimal("5.0"),
            memo="task payment",
            idempotency_key="key-123",
            zone_id="test-zone",
        )

    @pytest.mark.asyncio
    async def test_transfer_propagates_credits_error(self):
        from nexus.pay.credits import InsufficientCreditsError
        from nexus.pay.protocol import (
            CreditsPaymentProtocol,
            ProtocolError,
            ProtocolTransferRequest,
        )

        mock_service = AsyncMock()
        mock_service.transfer = AsyncMock(side_effect=InsufficientCreditsError("Not enough"))

        proto = CreditsPaymentProtocol(service=mock_service, zone_id="default")
        request = ProtocolTransferRequest(
            from_agent="alice",
            to="bob",
            amount=Decimal("1000.0"),
        )

        with pytest.raises(ProtocolError, match="Not enough"):
            await proto.transfer(request)


# =============================================================================
# 7. Error Propagation
# =============================================================================


class TestErrorPropagation:
    """Protocol errors preserve original exception info."""

    @pytest.mark.asyncio
    async def test_x402_error_chained(self):
        from nexus.pay.protocol import ProtocolError, ProtocolTransferRequest, X402PaymentProtocol
        from nexus.pay.x402 import X402Error

        mock_client = AsyncMock()
        mock_client.pay = AsyncMock(side_effect=X402Error("network timeout"))

        proto = X402PaymentProtocol(client=mock_client)
        request = ProtocolTransferRequest(
            from_agent="alice",
            to="0x1234567890abcdef1234567890abcdef12345678",
            amount=Decimal("1.0"),
        )

        with pytest.raises(ProtocolError) as exc_info:
            await proto.transfer(request)
        assert exc_info.value.__cause__ is not None

    @pytest.mark.asyncio
    async def test_credits_error_chained(self):
        from nexus.pay.credits import CreditsError
        from nexus.pay.protocol import (
            CreditsPaymentProtocol,
            ProtocolError,
            ProtocolTransferRequest,
        )

        mock_service = AsyncMock()
        mock_service.transfer = AsyncMock(side_effect=CreditsError("db error"))

        proto = CreditsPaymentProtocol(service=mock_service, zone_id="default")
        request = ProtocolTransferRequest(
            from_agent="alice",
            to="bob",
            amount=Decimal("1.0"),
        )

        with pytest.raises(ProtocolError) as exc_info:
            await proto.transfer(request)
        assert exc_info.value.__cause__ is not None
