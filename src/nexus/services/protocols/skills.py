"""Skills service protocol (Issue #1287: Extract domain services).

Defines the contract for skill distribution, subscription, and runtime.
Existing implementation: ``nexus.services.skill_service.SkillService``.

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md
    - Issue #1287: Extract NexusFS domain services from god object

Note:
    Method names are unprefixed (e.g., ``share`` not ``skills_share``).
    NexusFS delegation layer adds the ``skills_`` prefix for backward compat.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext


@runtime_checkable
class SkillsProtocol(Protocol):
    """Service contract for skill management operations.

    Three API groups:
    - Distribution: share / unshare skills with users, groups, or public
    - Subscription: discover / subscribe / unsubscribe from skill library
    - Runner: get_prompt_context / load skill content on-demand
    - Package: export / import_skill / validate_zip .skill (ZIP) packages
    """

    # Distribution
    def share(
        self,
        skill_path: str,
        share_with: str,
        context: OperationContext | None = None,
    ) -> str: ...

    def unshare(
        self,
        skill_path: str,
        unshare_from: str,
        context: OperationContext | None = None,
    ) -> bool: ...

    # Subscription
    def discover(
        self,
        context: OperationContext | None = None,
        filter: str = "all",
    ) -> list[Any]: ...

    def subscribe(
        self,
        skill_path: str,
        context: OperationContext | None = None,
    ) -> bool: ...

    def unsubscribe(
        self,
        skill_path: str,
        context: OperationContext | None = None,
    ) -> bool: ...

    # Runner
    def get_prompt_context(
        self,
        context: OperationContext | None = None,
        max_skills: int = 50,
    ) -> Any: ...

    def load(
        self,
        skill_path: str,
        context: OperationContext | None = None,
    ) -> Any: ...

    # Package management
    def export(
        self,
        skill_path: str | None = None,
        skill_name: str | None = None,
        output_path: str | None = None,
        format: str = "generic",
        include_dependencies: bool = False,
        context: OperationContext | None = None,
    ) -> dict[str, Any]: ...

    def import_skill(
        self,
        source_path: str | None = None,
        zip_bytes: bytes | str | None = None,
        zip_data: str | None = None,
        target_path: str | None = None,
        allow_overwrite: bool = False,
        context: OperationContext | None = None,
        tier: str | None = None,
    ) -> dict[str, Any]: ...

    def validate_zip(
        self,
        source_path: str | None = None,
        zip_bytes: bytes | str | None = None,
        zip_data: str | None = None,
        context: OperationContext | None = None,
    ) -> dict[str, Any]: ...
