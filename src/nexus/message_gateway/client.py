"""Gateway client for agents to send messages.

Provides a simple interface for LangGraph and other agents to send
messages through the Gateway API.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Constants
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3


class GatewayClient:
    """Client for sending messages through the Gateway API.

    Used by LangGraph and other agents to send replies to conversations.

    Usage:
        client = GatewayClient(
            base_url="http://localhost:8000",
            api_key="sk-xxx",
        )
        result = await client.send_message(
            text="Hello from agent!",
            user="agent:my-agent",
            session_id="discord:123:456",
            channel="discord",
        )
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """Initialize the Gateway client.

        Args:
            base_url: Nexus server URL (e.g., http://localhost:8000)
            api_key: API key for authentication
            timeout: Request timeout in seconds
        """
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    async def send_message(
        self,
        text: str,
        user: str,
        session_id: str,
        channel: str,
        *,
        parent_id: str | None = None,
        target: str | None = None,
        metadata: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Send a message through the Gateway API.

        Args:
            text: Message content
            user: Sender ID (e.g., "agent:my-agent")
            session_id: Session key (e.g., "discord:123:456")
            channel: Platform (e.g., "discord")
            parent_id: Optional parent message ID for threading
            target: Optional @mention hint
            metadata: Optional metadata dict
            correlation_id: Optional ID for agent-to-agent request/response

        Returns:
            Response dict with message_id, status, ts

        Raises:
            GatewayError: If the request fails
        """
        url = f"{self._base_url}/api/v2/gateway/messages"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        # Build payload
        payload: dict[str, Any] = {
            "text": text,
            "user": user,
            "role": "agent",  # Always "agent" for client-sent messages
            "session_id": session_id,
            "channel": channel,
        }
        if parent_id:
            payload["parent_id"] = parent_id
        if target:
            payload["target"] = target
        if metadata:
            payload["metadata"] = metadata
        if correlation_id:
            # Add correlation_id to metadata
            payload.setdefault("metadata", {})["correlation_id"] = correlation_id

        # Send with retries
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(url, json=payload, headers=headers)

                    if response.status_code == 201:
                        result = response.json()
                        logger.debug(f"Message sent: {result['message_id']}")
                        return result
                    elif response.status_code == 200:
                        # Duplicate message
                        return response.json()
                    else:
                        error_detail = response.json().get("detail", response.text)
                        raise GatewayError(
                            f"Gateway error {response.status_code}: {error_detail}"
                        )

            except httpx.TimeoutException as e:
                last_error = e
                logger.warning(f"Gateway timeout (attempt {attempt + 1}/{MAX_RETRIES})")
            except httpx.RequestError as e:
                last_error = e
                logger.warning(f"Gateway request error: {e}")
            except GatewayError:
                raise

        raise GatewayError(f"Gateway request failed after {MAX_RETRIES} retries: {last_error}")

    async def send_reply(
        self,
        text: str,
        user: str,
        session_id: str,
        channel: str,
        parent_id: str,
        *,
        target: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send a reply to a specific message.

        Convenience method for threading - sets parent_id.

        Args:
            text: Reply content
            user: Sender ID
            session_id: Session key
            channel: Platform
            parent_id: ID of message being replied to
            target: Optional @mention hint (defaults to original author)
            metadata: Optional metadata dict

        Returns:
            Response dict with message_id, status, ts
        """
        return await self.send_message(
            text=text,
            user=user,
            session_id=session_id,
            channel=channel,
            parent_id=parent_id,
            target=target,
            metadata=metadata,
        )


class GatewayError(Exception):
    """Error from Gateway API."""

    pass
