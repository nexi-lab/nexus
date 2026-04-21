"""Platform-specific daemon installer (#3804).

Two backends, picked by ``sys.platform``:

- **darwin**: ``~/Library/LaunchAgents/com.nexus.daemon.<profile>.plist`` loaded
  via ``launchctl bootstrap gui/<uid>``.
- **linux**: ``~/.config/systemd/user/nexus-daemon-<profile>.service`` loaded
  via ``systemctl --user daemon-reload && enable --now``.

Labels and unit names are **profile-scoped** so the same laptop can run N
daemons (one per enrollment) without the launchd/systemd unit ids colliding
— matches the per-profile state layout under ``~/.nexus/daemons/<profile>/``.

Other platforms (Windows, BSDs) raise ``NotImplementedError``. Both templates
live next to this module as package resources; rendering is a plain
``str.format()`` call.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from importlib import resources
from pathlib import Path

PLIST_LABEL_PREFIX = "com.nexus.daemon"
SYSTEMD_UNIT_PREFIX = "nexus-daemon"

_TEMPLATE_PACKAGE = "nexus.bricks.auth.daemon.templates"
_PLIST_TEMPLATE = "com.nexus.daemon.plist.j2"
_SYSTEMD_TEMPLATE = "nexus-daemon.service.j2"


def plist_label_for(profile: str) -> str:
    """Launchd label scoped by profile (``com.nexus.daemon.<profile>``)."""
    return f"{PLIST_LABEL_PREFIX}.{profile}"


def systemd_unit_name_for(profile: str) -> str:
    """Systemd unit filename scoped by profile (``nexus-daemon-<profile>.service``)."""
    # systemd allows dots in unit names but convention prefers dashes when
    # not using template units. Profiles already sanitize to [A-Za-z0-9.-].
    return f"{SYSTEMD_UNIT_PREFIX}-{profile}.service"


def _unsupported(what: str) -> NotImplementedError:
    return NotImplementedError(
        f"daemon {what} not supported on {sys.platform!r} (macOS + Linux only)"
    )


# ---------------------------------------------------------------------- macOS

_STDOUT_LOG_DARWIN_BASE = Path("~/Library/Logs").expanduser()


def _darwin_log_paths(profile: str) -> tuple[Path, Path]:
    base = _STDOUT_LOG_DARWIN_BASE
    return (base / f"nexus-daemon.{profile}.out.log", base / f"nexus-daemon.{profile}.err.log")


def install_plist_path(profile: str) -> Path:
    """Return the absolute path where the LaunchAgent plist lives (macOS only)."""
    if sys.platform != "darwin":
        raise _unsupported("launchd plist path")
    return Path("~/Library/LaunchAgents").expanduser() / f"{plist_label_for(profile)}.plist"


def render_plist(
    *,
    label: str,
    executable: str,
    config_path: Path,
    profile: str,
    stdout_path: Path,
    stderr_path: Path,
) -> str:
    """Render the launchd plist template. Works on any platform (tests rely on it)."""
    template = (
        resources.files(_TEMPLATE_PACKAGE).joinpath(_PLIST_TEMPLATE).read_text(encoding="utf-8")
    )
    return template.format(
        label=label,
        executable=executable,
        config_path=str(config_path),
        profile=profile,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
    )


def _install_darwin(*, executable: str, config_path: Path, profile: str) -> Path:
    stdout_log, stderr_log = _darwin_log_paths(profile)
    stdout_log.parent.mkdir(parents=True, exist_ok=True)
    label = plist_label_for(profile)
    plist_path = install_plist_path(profile)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(
        render_plist(
            label=label,
            executable=executable,
            config_path=config_path,
            profile=profile,
            stdout_path=stdout_log,
            stderr_path=stderr_log,
        ),
        encoding="utf-8",
    )
    uid = os.getuid()
    domain = f"gui/{uid}"
    service = f"{domain}/{label}"
    subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=True)
    subprocess.run(["launchctl", "enable", service], check=True)
    return plist_path


def _uninstall_darwin(profile: str) -> None:
    uid = os.getuid()
    label = plist_label_for(profile)
    service = f"gui/{uid}/{label}"
    # bootout: non-zero is fine if the service isn't loaded.
    subprocess.run(["launchctl", "bootout", service], check=False)
    with contextlib.suppress(FileNotFoundError):
        install_plist_path(profile).unlink()


# ---------------------------------------------------------------------- Linux


def install_systemd_unit_path(profile: str) -> Path:
    """Return the absolute path where the systemd user unit lives (Linux only)."""
    if sys.platform != "linux":
        raise _unsupported("systemd unit path")
    return Path("~/.config/systemd/user").expanduser() / systemd_unit_name_for(profile)


def render_systemd_unit(
    *,
    executable: str,
    config_path: Path,
    profile: str,
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
        profile=profile,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
    )


def _install_linux(*, executable: str, config_path: Path, profile: str) -> Path:
    log_dir = Path("~/.local/state/nexus/daemon").expanduser() / profile
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_log = log_dir / "out.log"
    stderr_log = log_dir / "err.log"

    unit_path = install_systemd_unit_path(profile)
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(
        render_systemd_unit(
            executable=executable,
            config_path=config_path,
            profile=profile,
            stdout_path=stdout_log,
            stderr_path=stderr_log,
        ),
        encoding="utf-8",
    )
    unit_name = systemd_unit_name_for(profile)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", unit_name], check=True)
    return unit_path


def _uninstall_linux(profile: str) -> None:
    unit_name = systemd_unit_name_for(profile)
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", unit_name],
        check=False,
    )
    with contextlib.suppress(FileNotFoundError):
        install_systemd_unit_path(profile).unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)


# --------------------------------------------------------------- public entry


def install(*, executable: str, config_path: Path, profile: str) -> Path:
    """Install + enable the daemon for the current platform.

    Returns the absolute path of the installed launchd plist / systemd unit.
    """
    if sys.platform == "darwin":
        return _install_darwin(executable=executable, config_path=config_path, profile=profile)
    if sys.platform == "linux":
        return _install_linux(executable=executable, config_path=config_path, profile=profile)
    raise _unsupported("install")


def uninstall(*, profile: str) -> None:
    """Disable + remove the daemon unit for the current platform + profile."""
    if sys.platform == "darwin":
        _uninstall_darwin(profile)
        return
    if sys.platform == "linux":
        _uninstall_linux(profile)
        return
    raise _unsupported("uninstall")
