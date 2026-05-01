"""Nexus unified extension metadata layer.

This package owns the manifest contract and discovery store shared by
plugins, connectors, and bricks. It MUST NOT import any extension impl
module — keeping that boundary lets introspection enumerate extensions
without triggering optional-dependency imports.
"""

__all__: list[str] = []
