from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from nexus.contracts.backend_features import BackendFeature
from nexus.core.path_utils import extract_zone_id, normalize_path

PROTOCOL_VERSION = "0.1.0"
POSIX_KEYS = ("read", "readdir", "stat", "write", "unlink", "mkdir", "rmdir", "rename", "glob")


def empty_posix() -> dict[str, bool]:
    return dict.fromkeys(POSIX_KEYS, False)


def readonly_posix() -> dict[str, bool]:
    posix = empty_posix()
    posix.update({"read": True, "readdir": True, "stat": True})
    return posix


def writable_posix() -> dict[str, bool]:
    posix = readonly_posix()
    posix.update({"write": True, "unlink": True, "mkdir": True, "rmdir": True, "rename": True})
    return posix


def _normalize_posix(posix: Mapping[str, Any]) -> dict[str, bool]:
    normalized = empty_posix()
    normalized.update({key: bool(value) for key, value in posix.items()})
    return normalized


def _feature_value(feature: BackendFeature | str) -> str:
    return feature.value if isinstance(feature, BackendFeature) else str(feature)


def _feature_values(features: Iterable[BackendFeature | str]) -> set[str]:
    return {_feature_value(feature) for feature in features}


def posix_from_backend_features(features: Iterable[BackendFeature | str]) -> dict[str, bool]:
    values = _feature_values(features)
    posix = readonly_posix()
    posix["readdir"] = BackendFeature.DIRECTORY_LISTING.value in values
    posix["unlink"] = BackendFeature.PATH_DELETE.value in values
    posix["rmdir"] = BackendFeature.PATH_DELETE.value in values
    posix["rename"] = BackendFeature.RENAME.value in values
    posix["write"] = bool(
        {
            BackendFeature.CAS.value,
            BackendFeature.ROOT_PATH.value,
            BackendFeature.MULTIPART_UPLOAD.value,
            BackendFeature.RESUMABLE_UPLOAD.value,
        }
        & values
    )
    return posix


def backend_capability_dict(
    *,
    backend_name: str = "",
    backend_type: str = "",
    features: Iterable[BackendFeature | str] = (),
    posix: Mapping[str, bool] | None = None,
    rust_native: bool = False,
    external: bool = False,
    extensions: Iterable[str] = (),
) -> dict[str, Any]:
    feature_values = sorted(_feature_values(features))
    extension_values = set(extensions)
    if BackendFeature.NATIVE_VERSIONING.value in feature_values:
        extension_values.add("x-nexus:versioning")
    return {
        "backend_name": backend_name,
        "backend_type": backend_type or backend_name,
        "posix": _normalize_posix(posix)
        if posix is not None
        else posix_from_backend_features(feature_values),
        "features": feature_values,
        "extensions": sorted(extension_values),
        "rust_native": bool(rust_native),
        "external": bool(external),
    }


def _mount_points_from_kernel(nexus_fs: Any) -> list[str]:
    kernel = getattr(nexus_fs, "_kernel", None)
    if kernel is None or not hasattr(kernel, "get_mount_points"):
        return []
    points: list[str] = []
    for canonical in kernel.get_mount_points():
        _zone, user_path = extract_zone_id(str(canonical))
        points.append(user_path)
    return sorted(set(points))


def _command_capabilities(exposed_methods: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "grep": {"supported": "grep" in exposed_methods, "filetype": {"allow": [], "deny": []}},
        "glob": {"supported": "glob" in exposed_methods, "filetype": {"allow": [], "deny": []}},
    }


def _workspace_capabilities(exposed_methods: Mapping[str, Any]) -> dict[str, bool]:
    return {
        "snapshot": "workspace_snapshot" in exposed_methods,
        "restore": "workspace_restore" in exposed_methods,
        "watch": "workspace_watch" in exposed_methods,
    }


def build_initialize_response_dict(
    *,
    nexus_fs: Any,
    exposed_methods: Mapping[str, Any],
    server_version: str,
    rust_mounts: Mapping[str, Mapping[str, Any]] | None = None,
    server_name: str = "nexus",
    protocol_version: str = PROTOCOL_VERSION,
) -> dict[str, Any]:
    backends: dict[str, dict[str, Any]] = {}
    for mount_point in _mount_points_from_kernel(nexus_fs):
        backends[mount_point] = backend_capability_dict(
            backend_name="",
            backend_type="",
            posix=readonly_posix(),
        )
    for mount_point, raw in (rust_mounts or {}).items():
        backends[normalize_path(mount_point)] = backend_capability_dict(
            backend_name=str(raw.get("backend_name") or ""),
            backend_type=str(raw.get("backend_type") or raw.get("backend_name") or ""),
            features=raw.get("features") or (),
            posix=raw.get("posix"),
            rust_native=bool(raw.get("rust_native", False)),
            external=bool(raw.get("external", False)),
            extensions=raw.get("extensions") or (),
        )
    if "/" not in backends:
        backends["/"] = backend_capability_dict(
            backend_name="",
            backend_type="",
            posix=readonly_posix(),
        )
    root_posix = dict(backends["/"]["posix"])
    extensions = sorted(
        {extension for backend in backends.values() for extension in backend.get("extensions", [])}
    )
    return {
        "server_name": server_name,
        "server_version": server_version,
        "protocol_version": protocol_version,
        "capabilities": {
            "posix": root_posix,
            "commands": _command_capabilities(exposed_methods),
            "workspace": _workspace_capabilities(exposed_methods),
            "backends": dict(sorted(backends.items())),
            "extensions": extensions,
        },
    }


def capability_for_path(
    capabilities: Mapping[str, Any] | None, path: str, capability: str
) -> bool | None:
    if not capabilities:
        return None
    normalized = normalize_path(path)
    backends = capabilities.get("backends") if isinstance(capabilities, Mapping) else None
    if isinstance(backends, Mapping):
        best_mount = ""
        best_posix: Mapping[str, Any] | None = None
        for mount_point, backend in backends.items():
            mount = normalize_path(str(mount_point))
            if (
                (normalized == mount or normalized.startswith(mount.rstrip("/") + "/"))
                and len(mount) > len(best_mount)
                and isinstance(backend, Mapping)
            ):
                posix = backend.get("posix")
                if isinstance(posix, Mapping):
                    best_mount = mount
                    best_posix = posix
        if best_posix is not None:
            if capability in best_posix:
                return bool(best_posix[capability])
            return None
    posix = capabilities.get("posix")
    if isinstance(posix, Mapping) and capability in posix:
        return bool(posix[capability])
    return None
