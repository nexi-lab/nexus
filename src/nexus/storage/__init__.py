"""Storage layer for Nexus - metadata store and SQLAlchemy models.

Heavy imports (views, models, FileContentCache) are lazily loaded to allow
lightweight environments (e.g. the slim remote-only Docker image) to import
storage without pulling in SQLAlchemy.
"""

import importlib
from typing import Any

_LAZY_STORAGE: dict[str, tuple[str, str]] = {
    "views": ("nexus.storage.views", "views"),
    "FileContentCache": ("nexus.storage.file_cache", "FileContentCache"),
    "FilePathModel": ("nexus.storage.models", "FilePathModel"),
    "FileMetadataModel": ("nexus.storage.models", "FileMetadataModel"),
    "UserModel": ("nexus.storage.models", "UserModel"),
    "UserOAuthAccountModel": ("nexus.storage.models", "UserOAuthAccountModel"),
    "ZoneModel": ("nexus.storage.models", "ZoneModel"),
    "ExternalUserServiceModel": ("nexus.storage.models", "ExternalUserServiceModel"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_STORAGE:
        module_path, attr_name = _LAZY_STORAGE[name]
        mod = importlib.import_module(module_path)
        # For the "views" module, return the module itself
        value = mod if attr_name == "views" else getattr(mod, attr_name)
        globals()[name] = value
        return value
    raise AttributeError(f"module 'nexus.storage' has no attribute {name!r}")


__all__ = [
    "FilePathModel",
    "FileMetadataModel",
    "UserModel",
    "UserOAuthAccountModel",
    "ZoneModel",
    "ExternalUserServiceModel",
    "FileContentCache",
    "views",
]
