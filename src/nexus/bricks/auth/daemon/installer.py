"""Platform-specific daemon installer (#3804).

Two backends, picked by ``sys.platform``:

- **darwin**: ``~/Library/LaunchAgents/com.nexus.daemon.plist`` loaded via
  ``launchctl bootstrap gui/<uid>``.
- **linux**: ``~/.config/systemd/user/nexus-daemon.service`` loaded via
  ``systemctl --user daemon-reload && enable --now``.

Other platforms (Windows, BSDs) raise ``NotImplementedError``. The two
templates live next to this module as package resources; both are filled
via ``str.format()`` — Jinja2 would be overkill for four placeholders.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from importlib import resources
from pathlib import Path

PLIST_LABEL = "com.nexus.daemon"
SYSTEMD_UNIT_NAME = "nexus-daemon.service"

_TEMPLATE_PACKAGE = "nexus.bricks.auth.daemon.templates"
_PLIST_TEMPLATE = "com.nexus.daemon.plist.j2"
_SYSTEMD_TEMPLATE = "nexus-daemon.service.j2"


def _unsupported(what: str) -> NotImplementedError:
    return NotImplementedError(
        f"daemon {what} not supported on {sys.platform!r} (macOS + Linux only)"
    )


# ---------------------------------------------------------------------- macOS

_STDOUT_LOG_DARWIN = Path("~/Library/Logs/nexus-daemon.out.log").expanduser()
_STDERR_LOG_DARWIN = Path("~/Library/Logs/nexus-daemon.err.log").expanduser()


def install_plist_path() -> Path:
    """Return the absolute path where the LaunchAgent plist lives (macOS only)."""
    if sys.platform != "darwin":
        raise _unsupported("launchd plist path")
    return Path("~/Library/LaunchAgents").expanduser() / f"{PLIST_LABEL}.plist"


def render_plist(
    *,
    executable: str,
    config_path: Path,
    stdout_path: Path,
    stderr_path: Path,
) -> str:
    """Render the launchd plist template. Works on any platform (tests rely on it)."""
    template = (
        resources.files(_TEMPLATE_PACKAGE).joinpath(_PLIST_TEMPLATE).read_text(encoding="utf-8")
    )
    return template.format(
        executable=executable,
        config_path=str(config_path),
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
    )


def _install_darwin(*, executable: str, config_path: Path) -> Path:
    _STDOUT_LOG_DARWIN.parent.mkdir(parents=True, exist_ok=True)
    plist_path = install_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(
        render_plist(
            executable=executable,
            config_path=config_path,
            stdout_path=_STDOUT_LOG_DARWIN,
            stderr_path=_STDERR_LOG_DARWIN,
        ),
        encoding="utf-8",
    )
    uid = os.getuid()
    domain = f"gui/{uid}"
    service = f"{domain}/{PLIST_LABEL}"
    subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=True)
    subprocess.run(["launchctl", "enable", service], check=True)
    return plist_path


def _uninstall_darwin() -> None:
    uid = os.getuid()
    service = f"gui/{uid}/{PLIST_LABEL}"
    # bootout: non-zero is fine if the service isn't loaded.
    subprocess.run(["launchctl", "bootout", service], check=False)
    with contextlib.suppress(FileNotFoundError):
        install_plist_path().unlink()


# ---------------------------------------------------------------------- Linux


def install_systemd_unit_path() -> Path:
    """Return the absolute path where the systemd user unit lives (Linux only)."""
    if sys.platform != "linux":
        raise _unsupported("systemd unit path")
    return Path("~/.config/systemd/user").expanduser() / SYSTEMD_UNIT_NAME


def render_systemd_unit(
    *,
    executable: str,
    config_path: Path,
    stdout_path: Path,
    stderr_path: Path,
) -> str:
    """Render the systemd unit template. Works on any platform (tests rely on it)."""
    template = (
        resources.files(_TEMPLATE_PACKAGE).joinpath(_SYSTEMD_TEMPLATE).read_text(encoding="utf-8")
    )
    return template.format(
        executable=executable,
        config_path=str(config_path),
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
    )


def _install_linux(*, executable: str, config_path: Path) -> Path:
    log_dir = Path("~/.local/state/nexus/daemon").expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_log = log_dir / "nexus-daemon.out.log"
    stderr_log = log_dir / "nexus-daemon.err.log"

    unit_path = install_systemd_unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(
        render_systemd_unit(
            executable=executable,
            config_path=config_path,
            stdout_path=stdout_log,
            stderr_path=stderr_log,
        ),
        encoding="utf-8",
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT_NAME], check=True)
    return unit_path


def _uninstall_linux() -> None:
    # disable --now stops + disables in one step; non-zero ok if not loaded.
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME],
        check=False,
    )
    with contextlib.suppress(FileNotFoundError):
        install_systemd_unit_path().unlink()
    # daemon-reload is optional here but keeps systemctl's state clean.
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)


# --------------------------------------------------------------- public entry


def install(*, executable: str, config_path: Path) -> Path:
    """Install + enable the daemon for the current platform.

    Returns the absolute path of the installed launchd plist / systemd unit.
    """
    if sys.platform == "darwin":
        return _install_darwin(executable=executable, config_path=config_path)
    if sys.platform == "linux":
        return _install_linux(executable=executable, config_path=config_path)
    raise _unsupported("install")


def uninstall() -> None:
    """Disable + remove the daemon unit for the current platform."""
    if sys.platform == "darwin":
        _uninstall_darwin()
        return
    if sys.platform == "linux":
        _uninstall_linux()
        return
    raise _unsupported("uninstall")
