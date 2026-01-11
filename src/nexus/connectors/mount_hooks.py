"""Mount hooks for connector integration.

Provides hooks that run when connectors are mounted, such as
auto-generating SKILL.md documentation.

Usage:
    >>> from nexus.connectors.mount_hooks import on_mount
    >>> from nexus.backends.gcalendar_connector import GoogleCalendarConnectorBackend
    >>>
    >>> # After adding mount to router
    >>> router.add_mount("/mnt/calendar", calendar_backend)
    >>>
    >>> # Run mount hooks
    >>> on_mount(calendar_backend, "/mnt/calendar", filesystem=nx)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.backends.backend import Backend
    from nexus.connectors.base import SkillDocMixin

logger = logging.getLogger(__name__)


def on_mount(
    backend: Backend,
    mount_path: str,
    filesystem: Any = None,
    skill_registry: Any = None,
) -> dict[str, Any]:
    """Run mount hooks for a backend.

    Called after a backend is mounted to perform setup tasks like
    generating .skill/ directory with documentation and examples.

    Args:
        backend: The backend being mounted
        mount_path: The mount path (e.g., "/mnt/calendar")
        filesystem: NexusFS instance for writing skill docs
        skill_registry: SkillRegistry instance for registration

    Returns:
        Dict with hook results:
        - skill_dir: Path to generated .skill directory
        - skill_md: Path to SKILL.md
        - examples: List of example file paths
        - skill_registered: Whether skill was registered

    Example:
        >>> result = on_mount(calendar_backend, "/mnt/calendar", filesystem=nx)
        >>> print(result)
        {'skill_dir': '/mnt/calendar/.skill', 'skill_md': '/mnt/calendar/.skill/SKILL.md', ...}
    """
    result: dict[str, Any] = {
        "skill_dir": None,
        "skill_md": None,
        "examples": [],
        "skill_registered": False,
    }

    # Check if backend has SkillDocMixin
    if hasattr(backend, "write_skill_docs") and hasattr(backend, "SKILL_NAME"):
        skill_mixin: SkillDocMixin = backend  # type: ignore

        # Set mount path on the mixin
        if hasattr(skill_mixin, "set_mount_path"):
            skill_mixin.set_mount_path(mount_path)

        # Set skill registry if provided
        if skill_registry and hasattr(skill_mixin, "set_skill_registry"):
            skill_mixin.set_skill_registry(skill_registry)

        # Generate and write .skill/ directory
        if filesystem and skill_mixin.SKILL_NAME:
            docs_result = skill_mixin.write_skill_docs(mount_path, filesystem)

            result["skill_md"] = docs_result.get("skill_md")
            result["examples"] = docs_result.get("examples", [])

            if result["skill_md"]:
                # Compute skill_dir from skill_md path
                import posixpath

                result["skill_dir"] = posixpath.dirname(result["skill_md"])
                logger.info(
                    f"Generated .skill/ for {skill_mixin.SKILL_NAME} at {result['skill_dir']}"
                )
                result["skill_registered"] = True

    return result


def generate_all_skill_docs(
    router: Any,
    filesystem: Any,
    skill_registry: Any = None,
) -> list[dict[str, Any]]:
    """Generate SKILL.md for all mounted connectors with SkillDocMixin.

    Iterates through all mounts in the router and generates SKILL.md
    for any backend that has SkillDocMixin.

    Args:
        router: PathRouter instance with mounts
        filesystem: NexusFS instance for writing SKILL.md
        skill_registry: SkillRegistry instance for registration

    Returns:
        List of results from on_mount for each connector

    Example:
        >>> results = generate_all_skill_docs(router, nx, skill_registry)
        >>> for r in results:
        ...     if r['skill_doc_path']:
        ...         print(f"Generated: {r['skill_doc_path']}")
    """
    results = []

    # Get all mounts from router
    if hasattr(router, "_mounts"):
        for mount_config in router._mounts:
            result = on_mount(
                backend=mount_config.backend,
                mount_path=mount_config.mount_point,
                filesystem=filesystem,
                skill_registry=skill_registry,
            )
            if result["skill_doc_path"]:
                results.append(result)

    return results
