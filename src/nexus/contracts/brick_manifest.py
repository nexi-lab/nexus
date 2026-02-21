"""BrickManifest — self-describing frozen dataclass for Nexus feature bricks.

Every brick that wants to participate in auto-discovery and startup
validation extends this base.  The base provides:

- Immutable identity (``name``, ``protocol``, ``version``)
- Dependency declaration (``dependencies``)
- Startup import verification via :meth:`verify_imports`
- Config schema metadata

Tier-neutral: no imports from ``nexus.core``, ``nexus.services``,
``nexus.bricks``, or any other tier-specific package.

Issue #1386 — BrickManifest dataclass for self-describing bricks.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BrickManifest:
    """Base manifest every Nexus brick extends.

    Fields
    ------
    name : str
        Machine-readable brick identifier (e.g. ``"rebac"``).
    protocol : str
        Name of the primary Protocol this brick satisfies.
    version : str
        SemVer version of the brick.
    description : str
        Human-readable summary.
    config_schema : dict
        JSON-schema-ish dict describing available configuration keys.
    dependencies : tuple[str, ...]
        Names of other bricks this brick depends on at runtime.
    required_modules : tuple[str, ...]
        Fully-qualified module names that **must** be importable.
    optional_modules : tuple[str, ...]
        Fully-qualified module names that **may** be importable.
    """

    name: str
    protocol: str
    version: str = "1.0.0"
    description: str = ""
    config_schema: dict[str, dict[str, object]] = field(
        default_factory=dict,
        hash=False,
    )
    dependencies: tuple[str, ...] = ()
    required_modules: tuple[str, ...] = ()
    optional_modules: tuple[str, ...] = ()

    # ------------------------------------------------------------------
    # Startup verification (lazy — called only when the brick activates)
    # ------------------------------------------------------------------

    def verify_imports(self) -> dict[str, bool]:
        """Check that declared modules are importable.

        Returns a dict mapping each module name to its import status.
        For *required* modules a failure is logged at ERROR; for
        *optional* modules a failure is logged at WARNING.

        This should be called **lazily** — only when the brick is about
        to be activated, not during discovery.
        """
        results: dict[str, bool] = {}

        for mod in self.required_modules:
            if _spec_exists(mod):
                results[mod] = True
            else:
                results[mod] = False
                logger.error("Required module missing for brick %s: %s", self.name, mod)

        for mod in self.optional_modules:
            if _spec_exists(mod):
                results[mod] = True
            else:
                results[mod] = False
                logger.warning("Optional module unavailable for brick %s: %s", self.name, mod)

        return results

    @property
    def all_required_present(self) -> bool:
        """Return ``True`` iff every required module is importable."""
        return all(_spec_exists(mod) for mod in self.required_modules)


def _spec_exists(module_name: str) -> bool:
    """Check if *module_name* is importable without loading it.

    ``importlib.util.find_spec`` raises ``ModuleNotFoundError`` when a
    parent package exists but the child does not.  We catch that here.
    """
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ModuleNotFoundError, ValueError):
        return False
