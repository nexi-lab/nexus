"""Backward-compat shim: nexus.services.permissions.namespace_factory.

Canonical location: ``nexus.rebac.namespace_factory``
"""

from nexus.rebac.namespace_factory import create_namespace_manager

__all__ = ["create_namespace_manager"]
