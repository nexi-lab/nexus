"""Skills Package Service — Export, Import, and Validation APIs.

Split from SkillService (Issue #2035, Phase 4.2) to keep each file
under ~700 LOC. Handles .skill (ZIP) package operations.

Uses the same narrow protocol dependencies as SkillService.
"""

import logging
from typing import TYPE_CHECKING, Any

from nexus.bricks.skills.exceptions import SkillValidationError
from nexus.services.protocols.rpc import rpc_expose

# Zip bomb protection limits
_MAX_ZIP_DECOMPRESSED_SIZE = 100 * 1024 * 1024  # 100 MB
_MAX_ZIP_FILE_COUNT = 500
_MAX_ZIP_SINGLE_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

if TYPE_CHECKING:
    from nexus.bricks.skills.service import SkillService
    from nexus.services.protocols.skill_deps import (
        SkillFilesystemProtocol,
        SkillPermissionProtocol,
    )

logger = logging.getLogger(__name__)


class SkillPackageService:
    """Package operations for skills: export, import, validate.

    Delegates permission checks and discovery to the parent SkillService.
    """

    def __init__(
        self,
        fs: "SkillFilesystemProtocol",
        perms: "SkillPermissionProtocol",
        skill_service: "SkillService",
    ):
        self._fs = fs
        self._perms = perms
        self._skill_service = skill_service

    # =========================================================================
    # Export
    # =========================================================================

    @rpc_expose(name="skills_export", description="Export a skill as a .skill (ZIP) package")
    def export(
        self,
        skill_path: str | None = None,
        skill_name: str | None = None,
        output_path: str | None = None,
        format: str = "generic",
        include_dependencies: bool = False,  # noqa: ARG002
        context: Any | None = None,
    ) -> dict[str, Any]:
        """Export a skill to .skill (ZIP) format."""
        import base64
        import io
        import json
        import zipfile

        self._skill_service._validate_context(context)
        assert context is not None

        if not skill_path and skill_name:
            user_skill_dir = f"/zone/{context.zone_id}/user/{context.user_id}/skill/"
            skill_path = f"{user_skill_dir}{skill_name}/"
            if not self._fs.sys_access(skill_path, context=context):
                skills = self._skill_service._discover_impl(context, filter="all")
                for s in skills:
                    if s.name == skill_name:
                        skill_path = s.path
                        break

        if not skill_path:
            raise SkillValidationError("Either skill_path or skill_name must be provided")

        self._skill_service._assert_can_read(skill_path, context)

        if not skill_path.endswith("/"):
            skill_path += "/"

        files_to_export: list[tuple[str, bytes]] = []

        def collect_files(dir_path: str, _prefix: str = "") -> None:
            try:
                items = self._fs.sys_readdir(dir_path, context=context)
                for item in items:
                    item_str = str(item)
                    if item_str.startswith(dir_path):
                        rel_path = item_str[len(dir_path) :]
                    else:
                        rel_path = item_str

                    full_path = (
                        f"{dir_path}{rel_path}" if not item_str.startswith("/") else item_str
                    )

                    try:
                        content = self._fs.sys_read(full_path, context=context)
                        if isinstance(content, str):
                            content = content.encode("utf-8")
                        files_to_export.append((rel_path, content))
                    except Exception:
                        logger.debug("Could not read %s, trying as directory", full_path)
                        collect_files(full_path + "/", rel_path + "/")
            except Exception:
                logger.debug("Could not list directory: %s", dir_path)

        collect_files(skill_path)

        if not files_to_export:
            raise SkillValidationError(f"No files found in skill: {skill_path}")

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            manifest = {
                "version": "1.0",
                "skill_path": skill_path,
                "files": [f[0] for f in files_to_export],
            }
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))

            for rel_path, content in files_to_export:
                zf.writestr(rel_path, content)

        zip_bytes = zip_buffer.getvalue()
        skill_name_from_path = skill_path.rstrip("/").split("/")[-1]

        if output_path:
            self._fs.sys_write(output_path, zip_bytes, context=context)
            return {
                "success": True,
                "path": output_path,
                "size_bytes": len(zip_bytes),
                "skill_name": skill_name_from_path,
                "format": format,
            }

        return {
            "success": True,
            "zip_data": base64.b64encode(zip_bytes).decode("ascii"),
            "size_bytes": len(zip_bytes),
            "skill_name": skill_name_from_path,
            "format": format,
            "filename": f"{skill_name_from_path}.skill",
        }

    # =========================================================================
    # Import
    # =========================================================================

    @rpc_expose(name="skills_import", description="Import a skill from a .skill (ZIP) package")
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
        """Import a skill from .skill (ZIP) format.

        Skills are always imported to the user's skill directory.
        """
        import base64
        import io
        import json
        import zipfile

        if tier is not None:
            import warnings

            warnings.warn(
                "tier parameter is deprecated and ignored",
                DeprecationWarning,
                stacklevel=2,
            )

        self._skill_service._validate_context(context)
        assert context is not None

        if zip_data and not zip_bytes:
            zip_bytes = zip_data

        if source_path:
            raw_zip_data = self._fs.sys_read(source_path, context=context)
            if isinstance(raw_zip_data, str):
                raw_zip_data = raw_zip_data.encode("utf-8")
        elif zip_bytes:
            raw_zip_data = base64.b64decode(zip_bytes) if isinstance(zip_bytes, str) else zip_bytes
        else:
            raise SkillValidationError("Either source_path or zip_data required")

        zip_buffer = io.BytesIO(raw_zip_data)
        files_imported: list[str] = []

        with zipfile.ZipFile(zip_buffer, mode="r") as zf:
            # Zip bomb protection
            file_list_all = zf.infolist()
            if len(file_list_all) > _MAX_ZIP_FILE_COUNT:
                raise SkillValidationError(
                    f"ZIP contains too many files ({len(file_list_all)} > {_MAX_ZIP_FILE_COUNT})"
                )
            total_size = sum(info.file_size for info in file_list_all)
            if total_size > _MAX_ZIP_DECOMPRESSED_SIZE:
                raise SkillValidationError(
                    f"ZIP decompressed size ({total_size} bytes) exceeds limit "
                    f"({_MAX_ZIP_DECOMPRESSED_SIZE} bytes)"
                )
            for info in file_list_all:
                if info.file_size > _MAX_ZIP_SINGLE_FILE_SIZE:
                    raise SkillValidationError(
                        f"File '{info.filename}' exceeds max size "
                        f"({info.file_size} > {_MAX_ZIP_SINGLE_FILE_SIZE} bytes)"
                    )

            try:
                manifest_data = zf.read("manifest.json")
                manifest = json.loads(manifest_data.decode("utf-8"))
            except Exception:
                logger.debug("No valid manifest.json in ZIP, using defaults")
                manifest = {}

            base_path = f"/zone/{context.zone_id}/user/{context.user_id}/skill/"

            file_list = zf.namelist()
            nested_skill_folder = None
            for name in file_list:
                if name.endswith("SKILL.md") and "/" in name:
                    nested_skill_folder = name.split("/")[0]
                    break

            skill_name = None
            if not target_path:
                manifest_skill_path = manifest.get("skill_path", "")
                if manifest_skill_path:
                    skill_name = manifest_skill_path.rstrip("/").split("/")[-1]
                elif nested_skill_folder:
                    skill_name = nested_skill_folder
                else:
                    raise SkillValidationError(
                        "Cannot determine skill name. ZIP must contain SKILL.md "
                        "in a named folder or have a manifest with skill_path."
                    )
                target_path = f"{base_path}{skill_name}/"

            if not target_path.endswith("/"):
                target_path += "/"

            skill_md_path = f"{target_path}SKILL.md"
            if self._fs.sys_access(skill_md_path, context=context) and not allow_overwrite:
                raise SkillValidationError(
                    f"Skill already exists at {target_path}. Set allow_overwrite=true to overwrite."
                )

            skill_name = target_path.rstrip("/").split("/")[-1]

            for name in file_list:
                if name == "manifest.json":
                    continue

                content = zf.read(name)

                if nested_skill_folder and name.startswith(nested_skill_folder + "/"):
                    rel_path = name[len(nested_skill_folder) + 1 :]
                else:
                    rel_path = name

                # Path traversal protection
                if ".." in rel_path or rel_path.startswith("/"):
                    raise SkillValidationError(
                        f"Illegal path in ZIP: '{rel_path}' (path traversal attempt)"
                    )

                if rel_path and not rel_path.endswith("/"):
                    file_path = f"{target_path}{rel_path}"
                    logger.info(
                        "[skills_import] Writing file: %s, user_id=%s, zone=%s",
                        file_path,
                        context.user_id,
                        context.zone_id,
                    )
                    try:
                        self._fs.sys_write(file_path, content, context=context)
                        files_imported.append(file_path)
                    except Exception as e:
                        logger.error(
                            "[skills_import] Failed to write %s: %s",
                            file_path,
                            e,
                            exc_info=True,
                        )
                        raise

            self._invalidate_skill_cache(target_path, base_path)

        return {
            "imported_skills": [skill_name],
            "skill_paths": [target_path],
        }

    # =========================================================================
    # Validate
    # =========================================================================

    @rpc_expose(name="skills_validate_zip", description="Validate a .skill (ZIP) package")
    def validate_zip(
        self,
        source_path: str | None = None,
        zip_bytes: bytes | str | None = None,
        zip_data: str | None = None,
        context: Any | None = None,
    ) -> dict[str, Any]:
        """Validate a .skill (ZIP) package without importing it."""
        import base64
        import io
        import json
        import zipfile

        self._skill_service._validate_context(context)
        assert context is not None

        if zip_data and not zip_bytes:
            zip_bytes = zip_data

        if source_path:
            raw_zip_data = self._fs.sys_read(source_path, context=context)
            if isinstance(raw_zip_data, str):
                raw_zip_data = raw_zip_data.encode("utf-8")
        elif zip_bytes:
            raw_zip_data = base64.b64decode(zip_bytes) if isinstance(zip_bytes, str) else zip_bytes
        else:
            raise SkillValidationError("Either source_path or zip_data required")

        errors: list[str] = []
        warnings: list[str] = []
        skills_found: list[str] = []
        has_skill_md = False

        try:
            zip_buffer = io.BytesIO(raw_zip_data)
            with zipfile.ZipFile(zip_buffer, mode="r") as zf:
                file_info_list = zf.infolist()
                if len(file_info_list) > _MAX_ZIP_FILE_COUNT:
                    errors.append(
                        f"ZIP contains too many files ({len(file_info_list)} > {_MAX_ZIP_FILE_COUNT})"
                    )
                    return {
                        "valid": False,
                        "skills_found": [],
                        "errors": errors,
                        "warnings": warnings,
                    }
                files = zf.namelist()

                if "manifest.json" in files:
                    try:
                        manifest_data = zf.read("manifest.json")
                        manifest = json.loads(manifest_data.decode("utf-8"))
                        skill_path = manifest.get("skill_path", "")
                        if skill_path:
                            skill_name = skill_path.rstrip("/").split("/")[-1]
                            skills_found.append(skill_name)
                    except Exception as e:
                        errors.append(f"Invalid manifest.json: {e}")
                else:
                    warnings.append("Missing manifest.json (will use default skill name)")

                for f in files:
                    if f.endswith("SKILL.md") or f == "SKILL.md":
                        has_skill_md = True
                        if not skills_found:
                            parts = f.rsplit("/", 1)
                            if len(parts) > 1:
                                skills_found.append(parts[0])
                            else:
                                skills_found.append("imported")
                        break

                if not has_skill_md:
                    errors.append("Missing SKILL.md file")

        except zipfile.BadZipFile as e:
            errors.append(f"Invalid ZIP file: {e}")
        except Exception as e:
            errors.append(f"Validation error: {e}")

        return {
            "valid": len(errors) == 0,
            "skills_found": skills_found,
            "errors": errors,
            "warnings": warnings,
        }

    def _invalidate_skill_cache(self, target_path: str, base_path: str) -> None:
        """Invalidate metadata cache for skill and parent directories."""
        try:
            parent_dir = target_path.rsplit("/", 2)[0] + "/" if "/" in target_path else base_path
            self._perms.invalidate_metadata_cache(target_path, parent_dir)
            logger.info("Invalidated cache for %s and parent %s", target_path, parent_dir)
        except Exception as e:
            logger.warning("Failed to invalidate cache: %s", e)
