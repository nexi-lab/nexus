"""Backward-compat shim: nexus.services.permissions.async_rebac_manager.

Canonical location: ``nexus.rebac.async_manager``
"""

from nexus.rebac.async_manager import AsyncReBACManager, create_async_engine_from_url

__all__ = ["AsyncReBACManager", "create_async_engine_from_url"]
