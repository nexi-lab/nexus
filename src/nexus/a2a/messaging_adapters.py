"""Adapters between A2A Messages and IPC MessageEnvelopes.

Provides bidirectional conversion so that A2A protocol messages can
flow through the IPC subsystem and vice versa (Decision 2 / #1587).

Moved from ``core/`` to ``a2a/`` because this module depends on
``nexus.a2a.models`` — the kernel must not import from services.
"""


from typing import Any, Literal

from nexus.a2a.models import DataPart, Message
from nexus.ipc.envelope import MessageEnvelope, MessageType


def a2a_message_to_envelope(
    message: Message,
    *,
    sender: str,
    recipient: str,
    msg_type: MessageType = MessageType.TASK,
    correlation_id: str | None = None,
    ttl_seconds: int | None = None,
) -> MessageEnvelope:
    """Convert an A2A Message to an IPC MessageEnvelope.

    The message content is stored in the envelope's ``payload`` dict,
    preserving role, parts, and metadata.
    """
    payload: dict[str, Any] = message.model_dump(mode="json")

    return MessageEnvelope.model_validate(
        {
            "from": sender,
            "to": recipient,
            "type": msg_type.value,
            "correlation_id": correlation_id,
            "ttl_seconds": ttl_seconds,
            "payload": payload,
        }
    )


def envelope_to_a2a_message(
    envelope: MessageEnvelope,
    *,
    role: Literal["user", "agent"] | None = None,
) -> Message:
    """Convert an IPC MessageEnvelope to an A2A Message.

    If the payload contains ``role`` and ``parts`` keys, it is treated
    as a structured A2A message.  Otherwise the entire payload is
    wrapped as a single ``DataPart``.

    Parameters
    ----------
    role:
        Override the message role.  When *None*, uses the role from
        the payload (defaulting to ``"agent"`` if absent).
    """
    payload = envelope.payload

    if "parts" in payload:
        # Structured A2A message payload
        effective_role = role or payload.get("role", "agent")
        return Message.model_validate(
            {
                "role": effective_role,
                "parts": payload["parts"],
                "metadata": payload.get("metadata"),
            }
        )

    # Untyped payload — wrap as a single DataPart
    effective_role = role or "agent"
    return Message(
        role=effective_role,
        parts=[DataPart(data=payload)],
    )
