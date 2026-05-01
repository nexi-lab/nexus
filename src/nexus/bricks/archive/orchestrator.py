"""Multi-zone archive orchestrator (#3793).

Wraps the single-zone ZoneExportService to produce one archive per zone
(or one across all zones, depending on caller). Output naming convention:
`<zone>-<utc-iso>.nexus`.

Cross-brick DI note: the orchestrator is decoupled from the portability
brick via local Protocol + dataclass definitions.  Callers inject a
concrete ZoneExportService at construction time; the orchestrator never
imports directly from nexus.bricks.portability.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# Local option model (mirrors ZoneExportOptions fields we set)
# ---------------------------------------------------------------------------


@dataclass
class ArchiveExportOptions:
    """Narrow options bag passed to the injected export service.

    Mirrors the subset of ``ZoneExportOptions`` consumed by the
    orchestrator, avoiding a direct cross-brick import.

    Attributes:
        output_path: Destination path for the ``.nexus`` bundle.
        strip_credentials: Strip credential placeholders before writing.
        sign: Ed25519-sign the bundle manifest.
        after_time: Only include entries modified after this timestamp.
        before_time: Only include entries modified before this timestamp.
    """

    output_path: Path
    strip_credentials: bool = True
    sign: bool = True
    after_time: datetime | None = None
    before_time: datetime | None = None


# ---------------------------------------------------------------------------
# Export-service protocol (structural sub-type of ZoneExportService)
# ---------------------------------------------------------------------------


class ZoneExportServiceProtocol(Protocol):
    """Minimal surface the orchestrator needs from a zone-export service."""

    def export_zone(self, zone_id: str, options: Any) -> Any:  # noqa: ANN401
        """Export a single zone and return its manifest."""
        ...


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class ArchiveOrchestrator:
    """Produce one ``.nexus`` archive per zone (or for all zones).

    Args:
        export_service: Concrete service that exports a single zone.
                        Must implement ``export_zone(zone_id, options)``.
        output_dir: Directory in which archives are written.
        zone_lister: Callable returning all zone IDs.  Required when
                     ``create_archives`` is called with ``zone_ids=None``.
    """

    def __init__(
        self,
        *,
        export_service: ZoneExportServiceProtocol,
        output_dir: Path,
        zone_lister: Callable[[], list[str]] | None = None,
    ) -> None:
        self.export_service = export_service
        self.output_dir = output_dir
        self.zone_lister = zone_lister

    def create_archives(
        self,
        *,
        zone_ids: list[str] | None,
        strip: bool,
        sign: bool,
        audit_from: datetime | None = None,
        audit_to: datetime | None = None,
    ) -> list[Any]:
        """Export one archive per zone.

        Args:
            zone_ids: Explicit list of zone IDs, or ``None`` to use the
                      injected ``zone_lister``.
            strip: Strip credentials from each bundle.
            sign: Sign each bundle with the configured Ed25519 key.
            audit_from: Lower bound of the audit-event time window.
            audit_to: Upper bound of the audit-event time window.

        Returns:
            List of ``ExportManifest`` instances, one per zone.

        Raises:
            ValueError: If ``zone_ids`` is ``None`` and no ``zone_lister``
                        was provided.
        """
        if zone_ids is None:
            if self.zone_lister is None:
                raise ValueError("zone_ids=None requires a zone_lister callable")
            zone_ids = self.zone_lister()

        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out: list[Any] = []
        for zone_id in zone_ids:
            output = self.output_dir / f"{zone_id}-{ts}.nexus"
            options = ArchiveExportOptions(
                output_path=output,
                strip_credentials=strip,
                sign=sign,
                after_time=audit_from,
                before_time=audit_to,
            )
            manifest = self.export_service.export_zone(zone_id, options)
            out.append(manifest)
        return out


__all__ = ["ArchiveExportOptions", "ArchiveOrchestrator", "ZoneExportServiceProtocol"]
