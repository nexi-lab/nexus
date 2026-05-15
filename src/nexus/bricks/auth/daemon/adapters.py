"""Non-file source adapters for gcloud/gh/gws (#3804).

These sources don't live in a single file — they're fetched by running a
CLI. The runner polls each `SubprocessSource` on an interval and feeds the
bytes into `Pusher.push_source(name, content=..., account_identifier=...)`,
which handles hash-based dedupe so unchanged output doesn't hit the server.

Failure policy: if the CLI returns non-zero, is missing, or times out,
``fetch()`` returns ``None`` and the runner skips this cycle. We never raise
into the runner — transient CLI blips shouldn't kill the daemon loop.

Account identification: each adapter carries a second command
``account_cmd`` that returns the active account label (e.g. ``gh auth status``
login name, ``gcloud config get-value account``). When the label cannot be
resolved, ``fetch_with_account()`` returns ``None`` and the runner skips
the push — we refuse to collapse multiple accounts under a shared
``"unknown"`` key because that silently overwrites earlier credentials.
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
    # Command that prints the active account label (e.g. email / username)
    # to stdout. Required — without it the runner cannot distinguish between
    # multiple enrolled accounts. The first non-empty stripped line is used.
    account_cmd: tuple[str, ...] | None = None

    def available(self) -> bool:
        """True if the first binary in ``cmd`` resolves on PATH."""
        return shutil.which(self.cmd[0]) is not None

    def fetch(self) -> bytes | None:
        """Run the CLI; return stdout bytes on success, ``None`` on failure.

        ``None`` covers: command not found, non-zero exit, timeout, or any
        OSError. The caller is expected to skip this cycle without raising.
        """
        return self._run_capture(self.cmd)

    def fetch_account_label(self) -> str | None:
        """Return the active account label (e.g. ``me@example.com``) or None.

        Runs ``account_cmd`` if set; returns the first non-empty stripped
        line of stdout. None on any failure or when ``account_cmd`` is None.
        """
        if self.account_cmd is None:
            return None
        out = self._run_capture(self.account_cmd)
        if out is None:
            return None
        try:
            decoded = out.decode("utf-8", errors="replace")
        except Exception:
            return None
        for line in decoded.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped
        return None

    def _run_capture(self, cmd: tuple[str, ...]) -> bytes | None:
        if shutil.which(cmd[0]) is None:
            log.debug("adapter %s: binary %r not on PATH", self.name, cmd[0])
            return None
        try:
            result = subprocess.run(  # noqa: S603 — cmd is operator-controlled
                list(cmd),
                capture_output=True,
                timeout=self.timeout_s,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("adapter %s: cmd %r failed: %s", self.name, cmd[0], exc)
            return None
        if result.returncode != 0:
            log.warning(
                "adapter %s: cmd %r exit=%d stderr=%r",
                self.name,
                cmd[0],
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
    account_cmd=("gcloud", "config", "get-value", "account"),
)
GH_SOURCE = SubprocessSource(
    name="gh",
    cmd=("gh", "auth", "token"),
    # `gh api user --jq .login` is reliable on modern gh (≥ 2.0) and single-
    # line. Falls back via fetch_account_label's first-non-empty-line rule
    # if the jq filter yields multiple lines for any reason.
    account_cmd=("gh", "api", "user", "--jq", ".login"),
)
# gws (Google Workspace): no single canonical CLI. This default matches the
# internal `gws` wrapper used in #3788; override via config in the future.
# Without a known account_cmd we deliberately leave it unset so the runner
# skips rather than merge accounts into a single profile row.
GWS_SOURCE = SubprocessSource(
    name="gws",
    cmd=("gws", "auth", "token"),
    account_cmd=None,
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
