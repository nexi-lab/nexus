"""macOS launchd installer for the Nexus daemon (#3804).

Writes ``~/Library/LaunchAgents/com.nexus.daemon.plist`` and loads it via
``launchctl bootstrap gui/<uid>``. Non-darwin platforms raise
``NotImplementedError``; Linux systemd-user support is a follow-up.

The plist template lives alongside this module as a package resource
(``templates/com.nexus.daemon.plist.j2``) and is loaded through
``importlib.resources``. Despite the ``.j2`` extension, rendering is a
plain ``str.format()`` call: four substitution slots don't justify pulling
Jinja2 into the daemon install path.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from importlib import resources
from pathlib import Path

PLIST_LABEL = "com.nexus.daemon"

_TEMPLATE_PACKAGE = "nexus.bricks.auth.daemon.templates"
_TEMPLATE_NAME = "com.nexus.daemon.plist.j2"

_STDOUT_LOG = Path("~/Library/Logs/nexus-daemon.out.log").expanduser()
_STDERR_LOG = Path("~/Library/Logs/nexus-daemon.err.log").expanduser()


def _require_darwin() -> None:
    if sys.platform != "darwin":
        raise NotImplementedError(
            "launchd installer is macOS-only; Linux systemd-user is a follow-up (#3804)"
        )


def install_plist_path() -> Path:
    """Return the absolute path where the LaunchAgent plist lives.

    macOS-only: raises ``NotImplementedError`` on other platforms.
    """
    _require_darwin()
    return Path("~/Library/LaunchAgents").expanduser() / f"{PLIST_LABEL}.plist"


def render_plist(
    *,
    executable: str,
    config_path: Path,
    stdout_path: Path,
    stderr_path: Path,
) -> str:
    """Render the plist template with the four path placeholders filled in.

    Works on any platform — callers (e.g. tests) rely on pure rendering
    without touching ``launchctl`` or the filesystem.
    """
    template = (
        resources.files(_TEMPLATE_PACKAGE).joinpath(_TEMPLATE_NAME).read_text(encoding="utf-8")
    )
    return template.format(
        executable=executable,
        config_path=str(config_path),
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
    )


def install(
    *,
    executable: str,
    config_path: Path,
) -> Path:
    """Write plist, bootstrap it into the user's launchd domain, enable it.

    Returns the path to the installed plist. macOS-only.
    """
    _require_darwin()

    # Ensure the logs directory exists so launchd can open the stdout/stderr paths.
    _STDOUT_LOG.parent.mkdir(parents=True, exist_ok=True)

    plist_path = install_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(
        render_plist(
            executable=executable,
            config_path=config_path,
            stdout_path=_STDOUT_LOG,
            stderr_path=_STDERR_LOG,
        ),
        encoding="utf-8",
    )

    uid = os.getuid()
    domain = f"gui/{uid}"
    service = f"{domain}/{PLIST_LABEL}"

    subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist_path)],
        check=True,
    )
    subprocess.run(
        ["launchctl", "enable", service],
        check=True,
    )
    return plist_path


def uninstall() -> None:
    """Unload the plist and delete it. macOS-only.

    ``bootout`` is run with ``check=False`` — if the service isn't currently
    loaded (e.g. a prior uninstall, or the user unloaded it manually) the
    command exits non-zero and that's fine; we still want to remove the file.
    """
    _require_darwin()

    uid = os.getuid()
    service = f"gui/{uid}/{PLIST_LABEL}"

    subprocess.run(
        ["launchctl", "bootout", service],
        check=False,
    )

    plist_path = install_plist_path()
    with contextlib.suppress(FileNotFoundError):
        plist_path.unlink()
