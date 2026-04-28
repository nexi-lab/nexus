"""BootIndexer — background workspace walker for sandbox profile (Issue #3786).

Walks the local workspace directory in a background daemon thread, feeding each
file to the search daemon.  On completion (or failure) it transitions
``health_state`` to ``"ready"`` so the server starts accepting traffic.

After the initial walk, ``FileWatcherIndexer`` takes over for incremental
updates.  ``BootIndexer`` does NOT start the watcher — that is
``SandboxBootstrapper``'s responsibility.

Design:
    - Single daemon thread (``threading.Thread(daemon=True)``) so it never
      blocks process shutdown.
    - ``start_async()`` returns immediately; walk happens in the background.
    - On any error the walk is aborted, an ERROR is logged, and the health
      state is still transitioned to ``"ready"`` (partial index is acceptable).
    - ``health_state`` is a plain mutable ``dict`` — callers may observe it
      from any thread.  The only write is ``health_state["status"] = "ready"``
      performed exactly once in the ``finally`` block.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class BootIndexer:
    """Background workspace walker for the sandbox profile.

    Args:
        workspace:     Absolute path to the local directory to walk.
        search_daemon: Object exposing ``index_file(path: Path)`` — called for
                       every file found under *workspace*.
        health_state:  Mutable dict with at least a ``"status"`` key.  Set to
                       ``{"status": "indexing"}`` by the caller before
                       ``start_async()`` is called.  This class writes
                       ``"ready"`` to ``health_state["status"]`` when
                       indexing finishes (or fails).
    """

    def __init__(
        self,
        workspace: Path,
        search_daemon: Any,
        health_state: dict[str, Any],
    ) -> None:
        self._workspace = workspace
        self._search_daemon = search_daemon
        self._health_state = health_state

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_async(self) -> None:
        """Spawn the background indexing thread and return immediately."""
        thread = threading.Thread(
            target=self._run,
            name="BootIndexer",
            daemon=True,
        )
        thread.start()

    # ------------------------------------------------------------------
    # Background worker
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Walk *workspace* and feed every file to the search daemon.

        Always transitions ``health_state["status"]`` to ``"ready"`` — even
        on failure — so the server is not stuck in the ``"indexing"`` state.
        """
        try:
            self._walk_and_index()
        except Exception as exc:
            logger.error(
                "[BootIndexer] walk failed for %s: %s",
                self._workspace,
                exc,
                exc_info=True,
            )
        finally:
            self._health_state["status"] = "ready"
            logger.info("[BootIndexer] indexing complete, health_state → ready")

    def _walk_and_index(self) -> None:
        """Walk the workspace directory and call ``index_file`` for each file."""
        if not self._workspace.is_dir():
            raise FileNotFoundError(
                f"workspace directory does not exist or is not a directory: {self._workspace}"
            )

        indexed = 0
        errors = 0
        for path in self._workspace.rglob("*"):
            if not path.is_file():
                continue
            try:
                self._search_daemon.index_file(path)
                indexed += 1
            except Exception as exc:
                errors += 1
                logger.warning("[BootIndexer] failed to index %s: %s", path, exc)

        logger.info(
            "[BootIndexer] walk finished: %d files indexed, %d errors",
            indexed,
            errors,
        )
