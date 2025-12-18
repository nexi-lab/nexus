"""Subscription manager for webhook event notifications."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from nexus.server.subscriptions.models import (
    Subscription,
    SubscriptionCreate,
    SubscriptionInfo,
    SubscriptionUpdate,
    WebhookPayload,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Constants
MAX_RETRIES = 3
RETRY_DELAYS = [1, 5, 30]  # seconds
WEBHOOK_TIMEOUT = 10.0  # seconds
MAX_CONSECUTIVE_FAILURES = 10  # Disable after this many failures


class SubscriptionManager:
    """Manages webhook subscriptions and event delivery."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        """Initialize subscription manager.

        Args:
            session_factory: SQLAlchemy session factory
        """
        self._session_factory = session_factory

    # =========================================================================
    # CRUD Operations
    # =========================================================================

    def create(
        self,
        tenant_id: str,
        data: SubscriptionCreate,
        created_by: str | None = None,
    ) -> Subscription:
        """Create a new subscription.

        Args:
            tenant_id: Tenant ID for isolation
            data: Subscription creation data
            created_by: User/agent who created the subscription

        Returns:
            Created subscription
        """
        from nexus.storage.models import SubscriptionModel

        with self._session_factory() as session:
            model = SubscriptionModel(
                subscription_id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                url=data.url,
                secret=data.secret,
                event_types=json.dumps(data.event_types),
                patterns=json.dumps(data.patterns) if data.patterns else None,
                name=data.name,
                description=data.description,
                custom_metadata=json.dumps(data.metadata) if data.metadata else None,
                enabled=1,
                created_by=created_by,
            )
            model.validate()
            session.add(model)
            session.commit()
            session.refresh(model)

            logger.info(
                f"Created subscription {model.subscription_id} for {data.url} "
                f"(tenant={tenant_id}, events={data.event_types})"
            )

            return self._to_subscription(model)

    def get(self, subscription_id: str, tenant_id: str) -> Subscription | None:
        """Get a subscription by ID.

        Args:
            subscription_id: Subscription ID
            tenant_id: Tenant ID for isolation

        Returns:
            Subscription if found, None otherwise
        """
        from nexus.storage.models import SubscriptionModel

        with self._session_factory() as session:
            model = (
                session.query(SubscriptionModel)
                .filter(
                    SubscriptionModel.subscription_id == subscription_id,
                    SubscriptionModel.tenant_id == tenant_id,
                )
                .first()
            )
            if model is None:
                return None
            return self._to_subscription(model)

    def list_subscriptions(
        self,
        tenant_id: str,
        enabled_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Subscription]:
        """List subscriptions for a tenant.

        Args:
            tenant_id: Tenant ID for isolation
            enabled_only: Only return enabled subscriptions
            limit: Maximum number of results
            offset: Offset for pagination

        Returns:
            List of subscriptions
        """
        from nexus.storage.models import SubscriptionModel

        with self._session_factory() as session:
            query = session.query(SubscriptionModel).filter(
                SubscriptionModel.tenant_id == tenant_id
            )
            if enabled_only:
                query = query.filter(SubscriptionModel.enabled == 1)
            query = query.order_by(SubscriptionModel.created_at.desc())
            query = query.limit(limit).offset(offset)

            return [self._to_subscription(m) for m in query.all()]

    def update(
        self,
        subscription_id: str,
        tenant_id: str,
        data: SubscriptionUpdate,
    ) -> Subscription | None:
        """Update a subscription.

        Args:
            subscription_id: Subscription ID
            tenant_id: Tenant ID for isolation
            data: Update data

        Returns:
            Updated subscription if found, None otherwise
        """
        from nexus.storage.models import SubscriptionModel

        with self._session_factory() as session:
            model = (
                session.query(SubscriptionModel)
                .filter(
                    SubscriptionModel.subscription_id == subscription_id,
                    SubscriptionModel.tenant_id == tenant_id,
                )
                .first()
            )
            if model is None:
                return None

            # Update fields
            if data.url is not None:
                model.url = data.url
            if data.event_types is not None:
                model.event_types = json.dumps(data.event_types)
            if data.patterns is not None:
                model.patterns = json.dumps(data.patterns) if data.patterns else None
            if data.secret is not None:
                model.secret = data.secret
            if data.name is not None:
                model.name = data.name
            if data.description is not None:
                model.description = data.description
            if data.metadata is not None:
                model.custom_metadata = json.dumps(data.metadata) if data.metadata else None
            if data.enabled is not None:
                model.enabled = 1 if data.enabled else 0
                # Reset failure count when re-enabling
                if data.enabled:
                    model.consecutive_failures = 0

            model.validate()
            session.commit()
            session.refresh(model)

            logger.info(f"Updated subscription {subscription_id}")
            return self._to_subscription(model)

    def delete(self, subscription_id: str, tenant_id: str) -> bool:
        """Delete a subscription.

        Args:
            subscription_id: Subscription ID
            tenant_id: Tenant ID for isolation

        Returns:
            True if deleted, False if not found
        """
        from nexus.storage.models import SubscriptionModel

        with self._session_factory() as session:
            result = (
                session.query(SubscriptionModel)
                .filter(
                    SubscriptionModel.subscription_id == subscription_id,
                    SubscriptionModel.tenant_id == tenant_id,
                )
                .delete()
            )
            session.commit()

            if result > 0:
                logger.info(f"Deleted subscription {subscription_id}")
                return True
            return False

    # =========================================================================
    # Event Broadcasting
    # =========================================================================

    async def broadcast(
        self,
        event_type: str,
        data: dict[str, Any],
        tenant_id: str,
    ) -> int:
        """Broadcast an event to matching subscriptions.

        Args:
            event_type: Event type (file_write, file_delete, etc.)
            data: Event data
            tenant_id: Tenant ID for isolation

        Returns:
            Number of webhooks triggered
        """
        logger.debug(
            f"broadcast() called: event={event_type}, tenant={tenant_id}, path={data.get('file_path', 'N/A')}"
        )

        # Get matching subscriptions (async to avoid blocking event loop)
        try:
            subscriptions = await self._get_matching_subscriptions(event_type, data, tenant_id)
        except Exception as e:
            logger.error(f"Error in _get_matching_subscriptions: {e}")
            return 0

        if not subscriptions:
            logger.debug(f"No matching subscriptions for {event_type} tenant={tenant_id}")
            return 0

        # Deliver webhooks concurrently (fire and forget for performance)
        tasks = []
        for sub in subscriptions:
            event_id = f"evt_{uuid.uuid4().hex[:16]}"
            tasks.append(
                asyncio.create_task(self._deliver_webhook(sub, event_type, data, event_id))
            )

        # Wait for all deliveries (with timeout to not block)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        logger.info(
            f"Broadcast {event_type} to {len(subscriptions)} subscriptions "
            f"(tenant={tenant_id}, path={data.get('file_path', 'N/A')})"
        )
        return len(subscriptions)

    async def _get_matching_subscriptions(
        self,
        event_type: str,
        data: dict[str, Any],
        tenant_id: str,
    ) -> list[Subscription]:
        """Get subscriptions matching the event (async, non-blocking).

        Args:
            event_type: Event type
            data: Event data
            tenant_id: Tenant ID

        Returns:
            List of matching subscriptions
        """
        return await asyncio.to_thread(
            self._get_matching_subscriptions_sync, event_type, data, tenant_id
        )

    def _get_matching_subscriptions_sync(
        self,
        event_type: str,
        data: dict[str, Any],
        tenant_id: str,
    ) -> list[Subscription]:
        """Sync implementation of _get_matching_subscriptions."""
        from nexus.storage.models import SubscriptionModel

        with self._session_factory() as session:
            # Get enabled subscriptions for tenant
            models = (
                session.query(SubscriptionModel)
                .filter(
                    SubscriptionModel.tenant_id == tenant_id,
                    SubscriptionModel.enabled == 1,
                )
                .all()
            )

            matching = []
            # For rename events, check both old_path and new_path
            file_path = data.get("file_path") or data.get("new_path") or data.get("old_path") or ""

            for model in models:
                # Check event type
                event_types = model.get_event_types()
                if event_type not in event_types:
                    continue

                # Check patterns - must match at least one pattern if specified
                patterns = model.get_patterns()
                if patterns and not any(self._match_pattern(file_path, p) for p in patterns):
                    continue

                matching.append(self._to_subscription(model))

            return matching

    @staticmethod
    def _match_pattern(path: str, pattern: str) -> bool:
        """Match a file path against a glob-style pattern.

        Supports ** for recursive directory matching.

        Args:
            path: File path to match
            pattern: Glob pattern (supports *, **, ?)

        Returns:
            True if path matches pattern
        """
        import re

        # Convert glob pattern to regex
        # **/ matches zero or more directories (including empty)
        # ** at end matches anything including subdirs
        # * matches any characters except /
        # ? matches single character except /
        regex_pattern = pattern

        # First, escape regex special chars except * and ?
        regex_pattern = re.escape(regex_pattern)

        # Handle ** patterns (must do before single *)
        # \*\*/ or \*\*\/ (escaped slash) -> match zero or more path segments
        regex_pattern = regex_pattern.replace(r"\*\*/", r"(?:.*/)?")  # **/ -> optionally match dirs
        regex_pattern = regex_pattern.replace(r"\*\*", ".*")  # ** at end -> match anything

        # Handle single * (now safe since ** is replaced)
        regex_pattern = regex_pattern.replace(r"\*", "[^/]*")  # * -> match non-slash chars
        regex_pattern = regex_pattern.replace(r"\?", "[^/]")  # ? -> match single non-slash

        regex_pattern = f"^{regex_pattern}$"

        return bool(re.match(regex_pattern, path))

    async def _deliver_webhook(
        self,
        subscription: Subscription,
        event_type: str,
        data: dict[str, Any],
        event_id: str,
    ) -> bool:
        """Deliver webhook with retries.

        Args:
            subscription: Target subscription
            event_type: Event type
            data: Event data
            event_id: Unique event ID

        Returns:
            True if delivered successfully
        """
        payload = WebhookPayload(
            id=event_id,
            event=event_type,
            timestamp=datetime.now(UTC),
            data=data,
            subscription=SubscriptionInfo(
                id=subscription.id,
                metadata=subscription.metadata,
            ),
        )

        payload_json = payload.model_dump_json()
        payload_bytes = payload_json.encode("utf-8")

        # Build headers
        headers = {
            "Content-Type": "application/json",
            "X-Nexus-Event": event_type,
            "X-Nexus-Delivery-Id": f"del_{uuid.uuid4().hex[:16]}",
        }

        # Add HMAC signature if secret is configured
        # Note: We don't have access to the secret from the Subscription model
        # Need to fetch it separately for signing
        signature = await self._compute_signature(subscription.id, payload_bytes)
        if signature:
            headers["X-Nexus-Signature"] = signature

        # Attempt delivery with retries
        logger.debug(f"_deliver_webhook: starting delivery to {subscription.url} for {event_type}")
        last_error: str | None = None

        for attempt in range(MAX_RETRIES):
            try:
                # Create a fresh client for each attempt to avoid connection state issues
                async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT) as client:
                    logger.debug(f"_deliver_webhook: attempt {attempt + 1} to {subscription.url}")
                    response = await client.post(
                        subscription.url,
                        content=payload_bytes,
                        headers=headers,
                    )

                    if response.status_code >= 200 and response.status_code < 300:
                        # Success
                        await self._update_delivery_status(
                            subscription.id, success=True, status_code=response.status_code
                        )
                        logger.debug(
                            f"Webhook delivered to {subscription.url} "
                            f"(status={response.status_code})"
                        )
                        return True
                    else:
                        last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                        logger.warning(
                            f"Webhook delivery failed for {subscription.url}: {last_error}"
                        )

            except httpx.TimeoutException:
                last_error = "Request timeout"
                logger.warning(f"Webhook timeout for {subscription.url}")
            except httpx.RequestError as e:
                last_error = str(e)
                logger.warning(f"Webhook error for {subscription.url}: {e}")
            except Exception as e:
                last_error = str(e)
                logger.error(f"Unexpected webhook error for {subscription.url}: {e}", exc_info=True)

            # Wait before retry (except on last attempt)
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAYS[attempt])

        # All retries failed
        await self._update_delivery_status(subscription.id, success=False, error=last_error)
        return False

    async def _compute_signature(self, subscription_id: str, payload: bytes) -> str | None:
        """Compute HMAC signature for payload (async, non-blocking).

        Args:
            subscription_id: Subscription ID to get secret
            payload: Payload bytes

        Returns:
            Signature string or None if no secret
        """
        return await asyncio.to_thread(self._compute_signature_sync, subscription_id, payload)

    def _compute_signature_sync(self, subscription_id: str, payload: bytes) -> str | None:
        """Sync implementation of _compute_signature."""
        from nexus.storage.models import SubscriptionModel

        with self._session_factory() as session:
            model = (
                session.query(SubscriptionModel)
                .filter(SubscriptionModel.subscription_id == subscription_id)
                .first()
            )
            if model is None or not model.secret:
                return None

            signature = hmac.new(
                model.secret.encode("utf-8"),
                payload,
                hashlib.sha256,
            ).hexdigest()
            return f"sha256={signature}"

    async def _update_delivery_status(
        self,
        subscription_id: str,
        success: bool,
        status_code: int | None = None,
        error: str | None = None,
    ) -> None:
        """Update subscription delivery status (async, non-blocking).

        Args:
            subscription_id: Subscription ID
            success: Whether delivery succeeded
            status_code: HTTP status code (reserved for future delivery logging)
            error: Error message if failed (reserved for future delivery logging)
        """
        await asyncio.to_thread(
            self._update_delivery_status_sync, subscription_id, success, status_code, error
        )

    def _update_delivery_status_sync(
        self,
        subscription_id: str,
        success: bool,
        status_code: int | None = None,  # noqa: ARG002 - reserved for future logging
        error: str | None = None,  # noqa: ARG002 - reserved for future logging
    ) -> None:
        """Sync implementation of _update_delivery_status."""
        from nexus.storage.models import SubscriptionModel

        with self._session_factory() as session:
            model = (
                session.query(SubscriptionModel)
                .filter(SubscriptionModel.subscription_id == subscription_id)
                .first()
            )
            if model is None:
                return

            model.last_delivery_at = datetime.now(UTC)
            model.last_delivery_status = "success" if success else "failed"

            if success:
                model.consecutive_failures = 0
            else:
                model.consecutive_failures += 1
                # Auto-disable after too many failures
                if model.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    model.enabled = 0
                    logger.warning(
                        f"Subscription {subscription_id} disabled after "
                        f"{MAX_CONSECUTIVE_FAILURES} consecutive failures"
                    )

            session.commit()

    # =========================================================================
    # Test Endpoint
    # =========================================================================

    async def test(self, subscription_id: str, tenant_id: str) -> dict[str, Any]:
        """Send a test event to a subscription.

        Args:
            subscription_id: Subscription ID
            tenant_id: Tenant ID for isolation

        Returns:
            Test result with status and response
        """
        subscription = self.get(subscription_id, tenant_id)
        if subscription is None:
            return {"success": False, "error": "Subscription not found"}

        test_data = {
            "file_path": "/test/webhook-test.txt",
            "size": 100,
            "etag": "test-etag",
            "version": 1,
            "created": True,
            "tenant_id": tenant_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "_test": True,
        }

        event_id = f"evt_test_{uuid.uuid4().hex[:8]}"
        success = await self._deliver_webhook(subscription, "file_write", test_data, event_id)

        return {
            "success": success,
            "event_id": event_id,
            "subscription_id": subscription_id,
        }

    # =========================================================================
    # Helpers
    # =========================================================================

    def _to_subscription(self, model: Any) -> Subscription:
        """Convert database model to Pydantic model."""
        return Subscription(
            id=model.subscription_id,
            tenant_id=model.tenant_id,
            url=model.url,
            event_types=model.get_event_types(),
            patterns=model.get_patterns() or None,
            name=model.name,
            description=model.description,
            metadata=model.get_metadata() or None,
            enabled=bool(model.enabled),
            last_delivery_at=model.last_delivery_at,
            last_delivery_status=model.last_delivery_status,
            consecutive_failures=model.consecutive_failures,
            created_at=model.created_at,
            updated_at=model.updated_at,
            created_by=model.created_by,
        )

    async def close(self) -> None:
        """Close the subscription manager and clean up resources.

        This is called during server shutdown.
        """
        logger.info("Closing SubscriptionManager")
        # Currently no persistent connections to close
        # Future: close any persistent HTTP clients or background tasks
