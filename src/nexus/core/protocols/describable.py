"""Describable protocol for recursive wrapper chain introspection (#1449).

Any brick that wraps another brick implementing the same Protocol MUST
implement ``describe()`` to return a human-readable chain description
for debugging.  Leaf bricks (non-wrappers) return their ``name``.

Example chain output::

    storage.describe()  # "cache \u2192 logging \u2192 s3"

Design reference:
    - NEXUS-LEGO-ARCHITECTURE.md PART 16 \u2014 Brick Composition Patterns
    - Recursive Wrapping Rule #3: every wrapper MUST implement describe()
"""


from typing import Protocol, runtime_checkable


@runtime_checkable
class Describable(Protocol):
    """Protocol for components that can describe their composition chain.

    Wrappers return ``"<layer> \u2192 {inner.describe()}"``.
    Leaf implementations return their ``name``.
    """

    def describe(self) -> str: ...
