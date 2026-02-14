"""Context manifest service protocol (Issue #1341).

Defines the contract for resolving context manifests — deterministic
pre-execution of sources before agent reasoning starts.

This is a service-layer protocol (not kernel) because it depends on
service-layer models (ContextSourceProtocol, ManifestResult).
The ``ManifestResolver`` in ``nexus.services.context_manifest.resolver``
is the current implementation.

References:
    - Issue #1341: Context manifest with deterministic pre-execution
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from nexus.services.context_manifest.models import (
    ContextSourceProtocol,
    ManifestResult,
)


@runtime_checkable
class ContextManifestProtocol(Protocol):
    """Protocol for resolving a context manifest.

    Implementations execute all declared sources (in parallel where possible),
    write result files to an output directory, and return a ``ManifestResult``.
    """

    async def resolve(
        self,
        sources: Sequence[ContextSourceProtocol],
        variables: dict[str, str],
        output_dir: Path,
    ) -> ManifestResult:
        """Resolve all sources and write results to *output_dir*.

        Args:
            sources: Sequence of context sources to resolve.
            variables: Template variable values for substitution.
            output_dir: Directory to write result files into.

        Returns:
            ManifestResult with all source results.

        Raises:
            ManifestResolutionError: If any required source fails.
            ValueError: If template variables are invalid.

        Note:
            Output files are written even when ``ManifestResolutionError``
            is raised, allowing callers to inspect partial results.
        """
        ...
