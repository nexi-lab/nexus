"""NexusFS LLM integration mixin — DEPRECATED.

Issue #1287 Phase B: This mixin has been replaced by:
- ``nexus.services.llm_service.LLMService`` (business logic)
- ``nexus.services.subsystems.llm_subsystem.LLMSubsystem`` (lifecycle management)

NexusFS no longer inherits from this mixin. The delegation methods with
``@rpc_expose`` are now directly on the NexusFS class.

This file is kept temporarily for backward compatibility. It will be
removed in a future release.
"""

from __future__ import annotations

import warnings


class NexusFSLLMMixin:
    """DEPRECATED: LLM mixin replaced by LLMSubsystem (Issue #1287)."""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        warnings.warn(
            "NexusFSLLMMixin is deprecated. Use LLMSubsystem instead (Issue #1287).",
            DeprecationWarning,
            stacklevel=2,
        )
