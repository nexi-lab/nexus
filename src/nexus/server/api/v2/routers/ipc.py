"""IPC REST API endpoints (Issue #1727, LEGO §8: Filesystem-as-IPC).

Provides REST access to the IPC subsystem:
    POST /api/v2/ipc/send                   — Send a message to an agent inbox
    GET  /api/v2/ipc/inbox/{agent_id}       — List messages in an agent's inbox
    GET  /api/v2/ipc/inbox/{agent_id}/count — Count messages in an agent's inbox
    POST /api/v2/ipc/provision/{agent_id}   — Provision IPC directories for an agent
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from nexus.bricks.ipc.conventions import validate_agent_id
from nexus.contracts.constants import ROOT_ZONE_ID

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/ipc", tags=["ipc"])

# ---------------------------------------------------------------------------
# Lazy imports (avoid circular imports with fastapi_server)
# ---------------------------------------------------------------------------


def _get_require_auth() -> Any:
    from nexus.server.dependencies import require_auth

    return require_auth


# ---------------------------------------------------------------------------
# Response / Request models
# ---------------------------------------------------------------------------


class SendMessageRequest(BaseModel):
    """Request body for sending an IPC message."""

    sender: str = Field(..., description="Sender agent ID")
    recipient: str = Field(..., description="Recipient agent ID")
    type: str = Field(default="task", description="Message type (task, response, event, cancel)")
    payload: dict[str, Any] = Field(default_factory=dict, description="Message payload")
    ttl_seconds: int | None = Field(default=None, description="Time-to-live in seconds")
    correlation_id: str | None = Field(
        default=None, description="Correlation ID for request/response"
    )


class SendMessageResponse(BaseModel):
    """Response after sending an IPC message."""

    message_id: str
    status: str = "sent"


class InboxMessageSummary(BaseModel):
    """Summary of a message in an agent's inbox."""

    filename: str


class InboxListResponse(BaseModel):
    """List of messages in an agent's inbox."""

    agent_id: str
    messages: list[InboxMessageSummary]
    total: int


class InboxCountResponse(BaseModel):
    """Count of messages in an agent's inbox."""

    agent_id: str
    count: int


class ProvisionResponse(BaseModel):
    """Response after provisioning IPC directories for an agent."""

    agent_id: str
    status: str = "provisioned"


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def _validate_agent_id(agent_id: str) -> str:
    """Validate agent_id at the REST boundary — rejects path traversal."""
    try:
        return validate_agent_id(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _check_agent_access(auth_result: dict[str, Any], agent_id: str) -> None:
    """Verify the caller is authorized to access the given agent's IPC resources.

    Admin users can access any agent. Non-admin callers must match the
    agent_id (via X-Agent-ID header or subject_id from auth).
    """
    if auth_result.get("is_admin", False):
        return
    caller_agent = auth_result.get("x_agent_id") or auth_result.get("subject_id")
    if caller_agent != agent_id:
        raise HTTPException(
            status_code=403,
            detail=f"Access denied: caller {caller_agent!r} cannot access agent {agent_id!r}",
        )


def _get_ipc_storage(request: Request) -> Any:
    """Get IPC storage driver from app.state."""
    storage = getattr(request.app.state, "ipc_storage_driver", None)
    if storage is None:
        raise HTTPException(status_code=503, detail="IPC storage not initialized")
    return storage


def _get_ipc_provisioner(request: Request) -> Any:
    """Get IPC AgentProvisioner from app.state."""
    provisioner = getattr(request.app.state, "ipc_provisioner", None)
    if provisioner is None:
        raise HTTPException(status_code=503, detail="IPC provisioner not initialized")
    return provisioner


def _get_zone_id(request: Request) -> str:
    """Get zone_id from app state."""
    return getattr(request.app.state, "zone_id", None) or ROOT_ZONE_ID


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/send", response_model=SendMessageResponse)
async def send_message(
    body: SendMessageRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    storage: Any = Depends(_get_ipc_storage),
    zone_id: str = Depends(_get_zone_id),
) -> SendMessageResponse:
    """Send a message to an agent's inbox.

    Creates a MessageEnvelope, writes it to the recipient's inbox,
    and optionally writes an outbox copy for the sender.
    Non-admin callers can only send as themselves (sender must match auth).
    """
    from nexus.bricks.ipc.delivery import MessageSender
    from nexus.bricks.ipc.envelope import MessageEnvelope, MessageType

    _validate_agent_id(body.sender)
    _validate_agent_id(body.recipient)

    # Authorization: non-admin callers must be the sender
    _check_agent_access(auth_result, body.sender)

    try:
        msg_type = MessageType(body.type)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid message type: {body.type!r}. "
            f"Valid types: {[t.value for t in MessageType]}",
        ) from exc

    envelope = MessageEnvelope.model_validate(
        {
            "from": body.sender,
            "to": body.recipient,
            "type": msg_type,
            "payload": body.payload,
            "ttl_seconds": body.ttl_seconds,
            "correlation_id": body.correlation_id,
        }
    )

    sender = MessageSender(storage, zone_id=zone_id)
    try:
        await sender.send(envelope)
    except Exception as exc:
        logger.error("IPC send failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to send message: {exc}") from exc

    return SendMessageResponse(message_id=envelope.id)


@router.get("/inbox/{agent_id}", response_model=InboxListResponse)
async def list_inbox(
    agent_id: str,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    storage: Any = Depends(_get_ipc_storage),
    zone_id: str = Depends(_get_zone_id),
) -> InboxListResponse:
    """List messages in an agent's inbox. Requires ownership or admin access."""
    from nexus.bricks.ipc.conventions import inbox_path

    _validate_agent_id(agent_id)
    _check_agent_access(auth_result, agent_id)

    path = inbox_path(agent_id)
    try:
        files = await storage.list_dir(path, zone_id)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=f"Inbox not found for agent {agent_id!r}"
        ) from exc

    messages = [InboxMessageSummary(filename=f) for f in files if f.endswith(".json")]
    return InboxListResponse(agent_id=agent_id, messages=messages, total=len(messages))


@router.get("/inbox/{agent_id}/count", response_model=InboxCountResponse)
async def count_inbox(
    agent_id: str,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    storage: Any = Depends(_get_ipc_storage),
    zone_id: str = Depends(_get_zone_id),
) -> InboxCountResponse:
    """Count messages in an agent's inbox. Requires ownership or admin access."""
    from nexus.bricks.ipc.conventions import inbox_path

    _validate_agent_id(agent_id)
    _check_agent_access(auth_result, agent_id)

    path = inbox_path(agent_id)
    try:
        count = await storage.count_dir(path, zone_id)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=f"Inbox not found for agent {agent_id!r}"
        ) from exc

    return InboxCountResponse(agent_id=agent_id, count=count)


@router.post("/provision/{agent_id}", response_model=ProvisionResponse)
async def provision_agent(
    agent_id: str,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    provisioner: Any = Depends(_get_ipc_provisioner),
) -> ProvisionResponse:
    """Provision IPC directories (inbox, outbox, processed, dead_letter) for an agent.

    Requires admin access.
    """
    _validate_agent_id(agent_id)

    # Provisioning is an admin-only operation
    if not auth_result.get("is_admin", False):
        raise HTTPException(status_code=403, detail="Agent provisioning requires admin access")

    try:
        await provisioner.provision(agent_id)
    except Exception as exc:
        logger.error("IPC provision failed for %s: %s", agent_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to provision agent: {exc}") from exc

    return ProvisionResponse(agent_id=agent_id)
