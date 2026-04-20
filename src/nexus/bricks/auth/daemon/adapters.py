"""Non-file source adapters for gcloud/gh/gws (#3804).

These sources don't live in a single file — they're fetched by running a
CLI. The runner polls each `SubprocessSource` on an interval and feeds the
bytes into `Pusher.push_source(name, content=...)`, which handles hash-based
dedupe so unchanged output doesn't hit the server.

Failure policy: if the CLI returns non-zero, is missing, or times out,
``fetch()`` returns ``None`` and the runner skips this cycle. We never raise
into the runner — transient CLI blips shouldn't kill the daemon loop.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubprocessSource:
    """A credential source fetched by running a CLI and capturing stdout."""

    name: str
    cmd: tuple[str, ...]
    timeout_s: float = 10.0

    def available(self) -> bool:
        """True if the first binary in ``cmd`` resolves on PATH."""
        return shutil.which(self.cmd[0]) is not None

    def fetch(self) -> bytes | None:
        """Run the CLI; return stdout bytes on success, ``None`` on failure.

        ``None`` covers: command not found, non-zero exit, timeout, or any
        OSError. The caller is expected to skip this cycle without raising.
        """
        if not self.available():
            log.debug("adapter %s: binary %r not on PATH", self.name, self.cmd[0])
            return None
        try:
            result = subprocess.run(  # noqa: S603 — cmd is operator-controlled
                list(self.cmd),
                capture_output=True,
                timeout=self.timeout_s,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("adapter %s: fetch failed: %s", self.name, exc)
            return None
        if result.returncode != 0:
            log.warning(
                "adapter %s: exit=%d stderr=%r",
                self.name,
                result.returncode,
                result.stderr[:200],
            )
            return None
        content = result.stdout.strip()
        if not content:
            return None
        return content


# Default adapter registry — wired into the daemon runner when enabled.
GCLOUD_SOURCE = SubprocessSource(
    name="gcloud",
    cmd=("gcloud", "auth", "application-default", "print-access-token"),
)
GH_SOURCE = SubprocessSource(
    name="gh",
    cmd=("gh", "auth", "token"),
)
# gws (Google Workspace): no single canonical CLI. This default matches the
# internal `gws` wrapper used in #3788; override via config in the future.
GWS_SOURCE = SubprocessSource(
    name="gws",
    cmd=("gws", "auth", "token"),
)

DEFAULT_SUBPROCESS_SOURCES: tuple[SubprocessSource, ...] = (
    GCLOUD_SOURCE,
    GH_SOURCE,
    GWS_SOURCE,
)


__all__ = [
    "DEFAULT_SUBPROCESS_SOURCES",
    "GCLOUD_SOURCE",
    "GH_SOURCE",
    "GWS_SOURCE",
    "SubprocessSource",
]
