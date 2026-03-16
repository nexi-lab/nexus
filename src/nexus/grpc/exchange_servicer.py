"""ExchangeService gRPC servicer — stub returning UNIMPLEMENTED.

Issue #2811: The ExchangeService proto defines 17 RPCs (identity,
payment, audit) but none are implemented as gRPC yet.  This stub
returns ``UNIMPLEMENTED`` with descriptive messages for all RPCs,
allowing clients to detect the service and get actionable feedback.

Once generated Python code from the exchange proto is available,
this servicer will extend the generated base class and delegate
to existing REST endpoint handlers.

Proto: proto/nexus/exchange/v1/exchange.proto
"""

import logging
from typing import Any

import grpc

logger = logging.getLogger(__name__)

# All RPCs defined in ExchangeService proto
_EXCHANGE_RPCS: dict[str, str] = {
    # Identity
    "VerifyIdentity": "Use POST /api/v2/identity/verify",
    "ListKeys": "Use GET /api/v2/identity/keys",
    "RotateKey": "Use POST /api/v2/identity/keys/rotate",
    "RevokeKey": "Use POST /api/v2/identity/keys/revoke",
    # Payment
    "GetBalance": "Use GET /api/v2/pay/balance",
    "Transfer": "Use POST /api/v2/pay/transfer",
    "BatchTransfer": "Use POST /api/v2/pay/transfer/batch",
    "Reserve": "Use POST /api/v2/pay/reserve",
    "CommitReservation": "Use POST /api/v2/pay/reserve/{id}/commit",
    "ReleaseReservation": "Use POST /api/v2/pay/reserve/{id}/release",
    "Meter": "Use POST /api/v2/pay/meter",
    "CanAfford": "Use GET /api/v2/pay/can-afford",
    # Audit
    "ListTransactions": "Use GET /api/v2/audit/transactions",
    "GetTransaction": "Use GET /api/v2/audit/transactions/{id}",
    "GetAggregations": "Use GET /api/v2/audit/transactions/aggregations",
    "VerifyIntegrity": "Use GET /api/v2/audit/integrity/{id}",
    "ExportTransactions": "Use GET /api/v2/audit/transactions/export",
}


class ExchangeServiceStub:
    """Stub servicer that returns UNIMPLEMENTED for all ExchangeService RPCs.

    This is registered as a generic service handler so that gRPC clients
    calling ExchangeService methods get a clear ``UNIMPLEMENTED`` status
    with a message pointing to the REST API equivalent.

    Once proto codegen is integrated, replace this with a proper servicer
    extending ``ExchangeServiceServicer``.
    """

    async def handle_unimplemented(
        self,
        _request: Any,  # noqa: ARG002
        context: grpc.aio.ServicerContext,
    ) -> None:
        """Handle any ExchangeService RPC with UNIMPLEMENTED."""
        method_name = "unknown"

        rest_hint = _EXCHANGE_RPCS.get(method_name, "Use the REST API instead")
        msg = f"ExchangeService.{method_name} is not yet implemented via gRPC. {rest_hint}."
        logger.debug("ExchangeService UNIMPLEMENTED: %s", method_name)
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, msg)


def get_unimplemented_rpcs() -> dict[str, str]:
    """Return the map of unimplemented RPC names to REST hints.

    Useful for ``nexus doctor`` to report ExchangeService status.
    """
    return dict(_EXCHANGE_RPCS)
