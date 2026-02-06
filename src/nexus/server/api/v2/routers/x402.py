"""x402 protocol API endpoints for Nexus Pay.

This module provides FastAPI routes for:
- Webhook endpoint for x402 payment confirmations
- Topup endpoint for agent credit purchases
- Configuration endpoint for x402 settings

Related: Issue #1206 (x402 protocol integration)
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/x402", tags=["x402"])


# =============================================================================
# Request/Response Models
# =============================================================================


class TopupRequest(BaseModel):
    """Request to initiate a credit topup via x402."""

    agent_id: str = Field(..., description="Agent to receive credits")
    amount: str = Field(..., description="Amount in USDC")
    tenant_id: str = Field(default="default", description="Tenant identifier")


class TopupResponse(BaseModel):
    """Response for topup initiation (402 with payment details)."""

    payment_required: bool = Field(default=True)
    amount: str = Field(..., description="Amount to pay in USDC")
    currency: str = Field(default="USDC")
    network: str = Field(default="eip155:8453", description="CAIP-2 network ID")
    address: str = Field(..., description="Recipient wallet address")
    description: str = Field(default="Credit topup")


class WebhookPayload(BaseModel):
    """x402 webhook payload for payment confirmation."""

    event: str = Field(..., description="Event type (e.g., payment.confirmed)")
    tx_hash: str = Field(..., description="Blockchain transaction hash")
    network: str = Field(..., description="CAIP-2 network ID")
    amount: str = Field(..., description="Amount in micro units")
    currency: str = Field(default="USDC")
    from_address: str = Field(..., alias="from", description="Sender wallet address")
    to_address: str = Field(..., alias="to", description="Recipient wallet address")
    timestamp: str = Field(..., description="ISO 8601 timestamp")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
    signature: str = Field(..., description="Webhook signature for verification")

    model_config = {"populate_by_name": True}


class WebhookResponse(BaseModel):
    """Response for webhook processing."""

    status: str = Field(..., description="Processing status")
    tx_id: str | None = Field(default=None, description="TigerBeetle transaction ID")
    message: str | None = Field(default=None, description="Status message")


class X402ConfigResponse(BaseModel):
    """x402 configuration for the server."""

    enabled: bool = Field(..., description="Whether x402 is enabled")
    network: str = Field(..., description="Supported network")
    facilitator_url: str = Field(..., description="x402 facilitator URL")
    wallet_address: str | None = Field(default=None, description="Server wallet address")


# =============================================================================
# Dependencies
# =============================================================================


def get_x402_client(request: Request) -> Any:
    """Get X402Client from app state."""
    x402_client = getattr(request.app.state, "x402_client", None)
    if not x402_client:
        raise HTTPException(
            status_code=503,
            detail="x402 not configured",
        )
    return x402_client


def get_credits_service(request: Request) -> Any:
    """Get CreditsService from app state."""
    credits_service = getattr(request.app.state, "credits_service", None)
    if not credits_service:
        raise HTTPException(
            status_code=503,
            detail="Credits service not available",
        )
    return credits_service


# =============================================================================
# Endpoints
# =============================================================================


@router.post("/webhook", response_model=WebhookResponse)
async def x402_webhook(
    payload: WebhookPayload,
    x402_client: Any = Depends(get_x402_client),
    credits_service: Any = Depends(get_credits_service),
) -> WebhookResponse:
    """Process x402 payment webhook and credit agent.

    This endpoint receives payment confirmations from the x402 facilitator
    and credits the agent's account in TigerBeetle.
    """
    from nexus.pay.x402 import X402Error

    try:
        # Convert Pydantic model to dict for processing
        webhook_dict = payload.model_dump(by_alias=True)

        tx_id = await x402_client.process_topup_webhook(
            webhook_payload=webhook_dict,
            credits_service=credits_service,
        )

        logger.info(
            "x402 webhook processed",
            extra={
                "tx_hash": payload.tx_hash,
                "tx_id": tx_id,
                "agent_id": payload.metadata.get("agent_id"),
            },
        )

        return WebhookResponse(
            status="credited",
            tx_id=tx_id,
            message="Payment processed and credits added",
        )

    except X402Error as e:
        logger.warning(
            "x402 webhook rejected",
            extra={"error": str(e), "tx_hash": payload.tx_hash},
        )
        raise HTTPException(status_code=400, detail=str(e)) from e

    except Exception as e:
        logger.error(
            "x402 webhook error",
            extra={"error": str(e), "tx_hash": payload.tx_hash},
        )
        raise HTTPException(status_code=500, detail="Internal error processing webhook") from e


@router.post("/topup", response_model=TopupResponse, status_code=402)
async def request_topup(
    request: TopupRequest,
    x402_client: Any = Depends(get_x402_client),
) -> TopupResponse:
    """Request a credit topup via x402.

    Returns a 402 Payment Required response with payment details.
    The client should then send USDC to the specified address.
    """
    from nexus.pay.x402 import X402Error

    try:
        # Get payment details from x402 client
        response = x402_client.payment_required_response(
            amount=Decimal(request.amount),
            description=f"Credit topup for agent {request.agent_id}",
        )

        # Extract payment details from response header
        import base64
        import json

        header_value = response.headers.get("X-Payment-Required", "")
        payload = json.loads(base64.b64decode(header_value).decode())

        return TopupResponse(
            payment_required=True,
            amount=payload["amount"],
            currency=payload["currency"],
            network=payload["network"],
            address=payload["address"],
            description=payload["description"],
        )

    except X402Error as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/config", response_model=X402ConfigResponse)
async def get_x402_config(
    request: Request,
) -> X402ConfigResponse:
    """Get x402 configuration.

    Returns the current x402 configuration for the server.
    """
    x402_client = getattr(request.app.state, "x402_client", None)

    if not x402_client:
        return X402ConfigResponse(
            enabled=False,
            network="base",
            facilitator_url="https://x402.org/facilitator",
            wallet_address=None,
        )

    return X402ConfigResponse(
        enabled=True,
        network=x402_client.network,
        facilitator_url=x402_client.facilitator_url,
        wallet_address=x402_client.wallet_address,
    )


# =============================================================================
# Module Exports
# =============================================================================

__all__ = ["router"]
