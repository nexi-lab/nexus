"""Namespace descendant-access checking — ReBAC visibility helper.

Hierarchical directory navigation: a subject can see a parent directory
if it has access to any descendant (even if deeply nested).
"""

from nexus.services.namespace.descendant_access import DescendantAccessChecker

__all__ = [
    "DescendantAccessChecker",
]
