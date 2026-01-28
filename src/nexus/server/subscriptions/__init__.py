"""Event subscription system for webhook notifications.

This module provides webhook-based event subscriptions that notify clients
when file events (write, delete, rename) occur in Nexus.

Example:
    from nexus.server.subscriptions import SubscriptionManager

    manager = SubscriptionManager(session_factory)

    # Create subscription
    sub = await manager.create(
        tenant_id="acme",
        url="https://my-app.com/webhooks/nexus",
        event_types=["file_write", "file_delete"],
        patterns=["/workspace/**/*"],
        secret="whsec_xxx"
    )

    # Broadcast event (called automatically from fire_event)
    await manager.broadcast(
        event_type="file_write",
        data={"file_path": "/workspace/doc.txt", ...},
        tenant_id="acme"
    )
"""

from nexus.server.subscriptions.manager import SubscriptionManager
from nexus.server.subscriptions.models import (
    Subscription,
    SubscriptionCreate,
    SubscriptionUpdate,
)

# Global subscription manager instance (set by FastAPI server)
_global_subscription_manager: SubscriptionManager | None = None


def get_subscription_manager() -> SubscriptionManager | None:
    """Get the global subscription manager instance.

    Returns:
        SubscriptionManager if initialized, None otherwise
    """
    return _global_subscription_manager


def set_subscription_manager(manager: SubscriptionManager | None) -> None:
    """Set the global subscription manager instance.

    Args:
        manager: SubscriptionManager instance or None to clear
    """
    global _global_subscription_manager
    _global_subscription_manager = manager


__all__ = [
    "SubscriptionManager",
    "Subscription",
    "SubscriptionCreate",
    "SubscriptionUpdate",
    "get_subscription_manager",
    "set_subscription_manager",
]
