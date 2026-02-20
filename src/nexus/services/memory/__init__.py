"""Memory services package — DEPRECATED, use nexus.bricks.memory instead.

Issue #2177: Memory service extracted to nexus.bricks.memory.
This shim provides backwards compatibility for external consumers.
"""

import warnings


def __getattr__(name: str):  # type: ignore[no-untyped-def]
    warnings.warn(
        f"nexus.services.memory.{name} is deprecated, use nexus.bricks.memory.{name}",
        DeprecationWarning,
        stacklevel=2,
    )
    import nexus.bricks.memory as _brick  # noqa: E402

    return getattr(_brick, name)
