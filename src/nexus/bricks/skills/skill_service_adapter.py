"""Gateway→Protocol adapter for SkillService (Issue #2035).

Canonical brick location is ``nexus.bricks.skills.service.SkillService``.
This adapter provides a gateway-accepting constructor that bridges
the gateway API to the brick protocol interface.
"""

import logging
from typing import Any, cast

from nexus.bricks.skills.service import SkillService as _SkillServiceImpl

logger = logging.getLogger(__name__)


class SkillService(_SkillServiceImpl):
    """Backward-compatible SkillService that accepts a NexusFSGateway.

    New code should use ``nexus.bricks.skills.service.SkillService`` directly
    with narrow protocol dependencies (fs, perms).

    This shim also provides ``export``, ``import_skill``, and ``validate_zip``
    by delegating to a lazily-created ``SkillPackageService``, so that it
    satisfies the full ``SkillsProtocol`` contract.
    """

    def __init__(
        self,
        gateway: Any | None = None,
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
        return self._get_pkg_svc().export(  # type: ignore[no-any-return]
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
        return self._get_pkg_svc().import_skill(  # type: ignore[no-any-return]
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
        return self._get_pkg_svc().validate_zip(  # type: ignore[no-any-return]
            source_path=source_path,
            zip_bytes=zip_bytes,
            zip_data=zip_data,
            context=context,
        )


class _GatewayFSAdapter:
    """Adapts NexusFSGateway to SkillFilesystemProtocol."""

    def __init__(self, gw: Any):
        self._gw = gw

    def sys_read(self, path: str, *, context: Any = None) -> bytes | str:
        return cast("bytes | str", self._gw.sys_read(path, context=context))

    def sys_write(self, path: str, content: bytes | str, *, context: Any = None) -> None:
        self._gw.sys_write(path, content, context=context)

    def sys_mkdir(self, path: str, *, context: Any = None) -> None:
        self._gw.sys_mkdir(path, context=context)

    def sys_readdir(self, path: str, *, context: Any = None) -> list[str]:
        return cast("list[str]", self._gw.sys_readdir(path, context=context))

    def sys_access(self, path: str, *, context: Any = None) -> bool:
        return cast(bool, self._gw.sys_access(path, context=context))


class _GatewayPermAdapter:
    """Adapts NexusFSGateway to SkillPermissionProtocol."""

    def __init__(self, gw: Any):
        self._gw = gw

    def rebac_check(
        self,
        *,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        zone_id: str | None = None,
    ) -> bool:
        return cast(
            bool,
            self._gw.rebac_check(
                subject=subject, permission=permission, object=object, zone_id=zone_id
            ),
        )

    def rebac_create(
        self,
        *,
        subject: tuple[str, str] | tuple[str, str, str],
        relation: str,
        object: tuple[str, str],
        zone_id: str | None = None,
        context: Any = None,
    ) -> dict[str, Any] | None:
        # Gateway accepts 2-tuple; narrow the 3-tuple at the adapter boundary
        subj: Any = subject
        return cast(
            "dict[str, Any] | None",
            self._gw.rebac_create(
                subject=subj, relation=relation, object=object, zone_id=zone_id, context=context
            ),
        )

    def rebac_list_tuples(
        self,
        *,
        subject: tuple[str, str] | None = None,
        relation: str | None = None,
        object: tuple[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        return cast(
            "list[dict[str, Any]]",
            self._gw.rebac_list_tuples(subject=subject, relation=relation, object=object),
        )

    def rebac_delete_object_tuples(
        self,
        *,
        object: tuple[str, str],
        zone_id: str | None = None,
    ) -> int:
        return cast(int, self._gw.rebac_delete_object_tuples(object=object, zone_id=zone_id))

    def invalidate_metadata_cache(self, *paths: str) -> None:
        invalidate = getattr(self._gw, "invalidate_metadata_cache", None)
        if invalidate is not None:
            invalidate(*paths)

    @property
    def rebac_manager(self) -> Any:
        return getattr(self._gw, "rebac_manager", None)
