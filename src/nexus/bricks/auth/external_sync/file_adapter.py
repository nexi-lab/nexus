"""FileAdapter — base class for external CLIs with parseable config files.

Subclasses declare which files to read and how to parse them. The base
class handles all I/O: detect (file exists), sync (read + parse + classify
errors), and graceful degradation on missing/unreadable/malformed files.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from pathlib import Path

from nexus.bricks.auth.external_sync.base import (
    ExternalCliSyncAdapter,
    SyncedProfile,
    SyncResult,
)

logger = logging.getLogger(__name__)


class FileAdapter(ExternalCliSyncAdapter):
    """Base class for config-file-based sync adapters.

    Subclasses implement paths() and parse_file() only.
    """

    sync_ttl_seconds: float = 60.0  # file reads are cheap

    @abstractmethod
    def paths(self) -> list[Path]:
        """Config file paths to read, in priority order."""
        ...

    @abstractmethod
    def parse_file(self, path: Path, content: str) -> list[SyncedProfile]:
        """Parse a config file into discovered profiles.

        Raise ValueError or similar on malformed content — the base class
        catches it and returns a degraded SyncResult.
        """
        ...

    async def detect(self) -> bool:
        """Return True if any config file from paths() exists and is readable."""
        for p in self.paths():
            try:
                if p.exists() and p.is_file():
                    return True
            except OSError:
                continue
        return False

    async def sync(self) -> SyncResult:
        """Read config files and parse profiles.

        Reads files in priority order from paths(). Aggregates all
        discovered profiles. Returns degraded SyncResult on I/O or
        parse errors rather than raising.
        """
        readable_paths = self.paths()
        if not readable_paths:
            return SyncResult(
                adapter_name=self.adapter_name,
                error="No config file paths configured",
            )

        all_profiles: list[SyncedProfile] = []
        errors: list[str] = []
        any_read = False

        for path in readable_paths:
            try:
                content = path.read_text(encoding="utf-8")
                any_read = True
            except FileNotFoundError:
                continue
            except PermissionError as exc:
                errors.append(f"{path}: permission denied ({exc})")
                continue
            except OSError as exc:
                errors.append(f"{path}: {exc}")
                continue

            if not content.strip():
                continue

            try:
                profiles = self.parse_file(path, content)
                all_profiles.extend(profiles)
            except Exception as exc:
                errors.append(f"{path}: parse error: {exc}")

        if not any_read and not all_profiles:
            paths_str = ", ".join(str(p) for p in readable_paths)
            return SyncResult(
                adapter_name=self.adapter_name,
                error=f"No readable config files found ({paths_str})",
            )

        error_msg = "; ".join(errors) if errors else None
        return SyncResult(
            adapter_name=self.adapter_name,
            profiles=all_profiles,
            error=error_msg,
        )
