"""Backward-compatible shim for SkillService (Issue #2035).

Canonical location is now ``nexus.skills.service.SkillService``.
This module provides a gateway-accepting constructor for backward
compatibility with code that creates ``SkillService(gateway=gw)``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nexus.skills.service import SkillService as _SkillServiceImpl

if TYPE_CHECKING:
    from nexus.services.gateway import NexusFSGateway

logger = logging.getLogger(__name__)


class SkillService(_SkillServiceImpl):
    """Backward-compatible SkillService that accepts a NexusFSGateway.

    New code should use ``nexus.skills.service.SkillService`` directly
    with narrow protocol dependencies (fs, perms).

    This shim also provides ``export``, ``import_skill``, and ``validate_zip``
    by delegating to a lazily-created ``SkillPackageService``, so that it
    satisfies the full ``SkillsProtocol`` contract.
    """

    def __init__(
        self,
        gateway: NexusFSGateway | None = None,
        *,
        fs: Any | None = None,
        perms: Any | None = None,
        create_system_context: Any | None = None,
    ):
        if gateway is not None and fs is None:
            # Legacy path: adapt gateway to narrow protocols
            super().__init__(
                fs=_GatewayFSAdapter(gateway),
                perms=_GatewayPermAdapter(gateway),
                create_system_context=create_system_context,
            )
        elif fs is not None and perms is not None:
            # New path: direct protocol injection
            super().__init__(fs=fs, perms=perms, create_system_context=create_system_context)
        else:
            raise TypeError("SkillService requires either gateway or (fs, perms)")
        self._pkg_svc: Any | None = None

    def _get_pkg_svc(self) -> Any:
        """Lazily create SkillPackageService for package operations."""
        if self._pkg_svc is None:
            from nexus.bricks.skills.package_service import SkillPackageService

            self._pkg_svc = SkillPackageService(
                fs=self._fs,
                perms=self._perms,
                skill_service=self,
            )
        return self._pkg_svc

    def export(
        self,
        skill_path: str | None = None,
        skill_name: str | None = None,
        output_path: str | None = None,
        format: str = "generic",
        include_dependencies: bool = False,
        context: Any | None = None,
    ) -> dict[str, Any]:
        return self._get_pkg_svc().export(
            skill_path=skill_path,
            skill_name=skill_name,
            output_path=output_path,
            format=format,
            include_dependencies=include_dependencies,
            context=context,
        )

    def import_skill(
        self,
        source_path: str | None = None,
        zip_bytes: bytes | str | None = None,
        zip_data: str | None = None,
        target_path: str | None = None,
        allow_overwrite: bool = False,
        context: Any | None = None,
        tier: str | None = None,
    ) -> dict[str, Any]:
        return self._get_pkg_svc().import_skill(
            source_path=source_path,
            zip_bytes=zip_bytes,
            zip_data=zip_data,
            target_path=target_path,
            allow_overwrite=allow_overwrite,
            context=context,
            tier=tier,
        )

    def validate_zip(
        self,
        source_path: str | None = None,
        zip_bytes: bytes | str | None = None,
        zip_data: str | None = None,
        context: Any | None = None,
    ) -> dict[str, Any]:
        return self._get_pkg_svc().validate_zip(
            source_path=source_path,
            zip_bytes=zip_bytes,
            zip_data=zip_data,
            context=context,
        )


class _GatewayFSAdapter:
    """Adapts NexusFSGateway to SkillFilesystemProtocol."""

    def __init__(self, gw: NexusFSGateway):
        self._gw = gw

    def read(self, path: str, *, context: Any = None) -> bytes | str:
        return self._gw.read(path, context=context)

    def write(self, path: str, content: bytes | str, *, context: Any = None) -> None:
        self._gw.write(path, content, context=context)

    def mkdir(self, path: str, *, context: Any = None) -> None:
        self._gw.mkdir(path, context=context)

    def list(self, path: str, *, context: Any = None) -> list[str]:
        return self._gw.list(path, context=context)

    def exists(self, path: str, *, context: Any = None) -> bool:
        return self._gw.exists(path, context=context)


class _GatewayPermAdapter:
    """Adapts NexusFSGateway to SkillPermissionProtocol."""

    def __init__(self, gw: NexusFSGateway):
        self._gw = gw

    def rebac_check(
        self,
        *,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        zone_id: str | None = None,
    ) -> bool:
        return self._gw.rebac_check(
            subject=subject, permission=permission, object=object, zone_id=zone_id
        )

    def rebac_create(
        self,
        *,
        subject: tuple[str, ...],
        relation: str,
        object: tuple[str, str],
        zone_id: str | None = None,
        context: Any = None,
    ) -> dict[str, Any] | None:
        return self._gw.rebac_create(
            subject=subject, relation=relation, object=object, zone_id=zone_id, context=context
        )  # type: ignore[arg-type]

    def rebac_list_tuples(
        self,
        *,
        subject: tuple[str, str] | None = None,
        relation: str | None = None,
        object: tuple[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        return self._gw.rebac_list_tuples(subject=subject, relation=relation, object=object)

    def rebac_delete_object_tuples(
        self,
        *,
        object: tuple[str, str],
        zone_id: str | None = None,
    ) -> int:
        return self._gw.rebac_delete_object_tuples(object=object, zone_id=zone_id)

    def invalidate_metadata_cache(self, *paths: str) -> None:
        self._gw.invalidate_metadata_cache(*paths)

    @property
    def rebac_manager(self) -> Any:
        return self._gw.rebac_manager
