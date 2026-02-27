"""DynamicViewerReadHook — column-level CSV filtering (INTERCEPT read).

Issue #625: Lives in services/rebac/ (service-layer, not kernel).
"""

import logging
from collections.abc import Callable
from typing import Any

from nexus.contracts.vfs_hooks import ReadHookContext

logger = logging.getLogger(__name__)


class DynamicViewerReadHook:
    """Post-read hook that applies column-level CSV filtering.

    Only activates for .csv files when ReBAC dynamic_viewer grants exist.

    Dependencies injected at construction:
      - get_subject:            (context) -> tuple[str, str] | None
      - get_viewer_config:      (subject, file_path) -> dict | None
      - apply_filter:           (data, column_config, file_format) -> dict
    """

    def __init__(
        self,
        get_subject: Callable[[Any], tuple[str, str] | None],
        get_viewer_config: Callable[[tuple[str, str], str], dict | None],
        apply_filter: Callable[[str, dict, str], dict[str, Any]],
    ) -> None:
        self._get_subject = get_subject
        self._get_viewer_config = get_viewer_config
        self._apply_filter = apply_filter

    @property
    def name(self) -> str:
        return "dynamic_viewer"

    def on_post_read(self, ctx: ReadHookContext) -> None:
        if ctx.content is None:
            return

        # Only process CSV files
        if not ctx.path.lower().endswith(".csv"):
            return

        subject = self._get_subject(ctx.context)
        if not subject:
            return

        column_config = self._get_viewer_config(subject, ctx.path)
        if not column_config:
            return

        logger.info(
            f"[DynamicViewerHook] Applying filter for {subject} on {ctx.path}: {column_config}"
        )

        content_str = ctx.content.decode("utf-8") if isinstance(ctx.content, bytes) else ctx.content
        result = self._apply_filter(content_str, column_config, "csv")

        filtered = result["filtered_data"]
        if isinstance(filtered, str):
            ctx.content = filtered.encode("utf-8")
        elif isinstance(filtered, bytes):
            ctx.content = filtered
        else:
            ctx.content = str(filtered).encode("utf-8")

        logger.info(f"[DynamicViewerHook] Successfully filtered {ctx.path}")
