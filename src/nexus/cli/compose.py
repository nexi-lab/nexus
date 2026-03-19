"""Docker Compose runner for CLI infrastructure commands.

Thin wrapper around ``docker compose`` CLI providing:
- Compose file discovery
- Profile management
- Subprocess execution with signal forwarding
- Error translation to user-friendly messages
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Default profiles activated when none are specified.
DEFAULT_PROFILES: tuple[str, ...] = ("server", "cache", "events")

# All valid profile names (must match docker-compose.yml service profiles).
VALID_PROFILES: frozenset[str] = frozenset({"server", "mcp", "cache", "events", "test", "all"})


class ComposeError(Exception):
    """Raised when a Docker Compose operation fails."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _find_compose_file(project_dir: Path | None = None) -> Path:
    """Locate ``docker-compose.yml`` starting from *project_dir*.

    Walks up the directory tree until it finds the file or reaches the
    filesystem root.
    """
    start = project_dir or Path.cwd()
    current = start.resolve()
    for _ in range(20):  # safety limit
        candidate = current / "docker-compose.yml"
        if candidate.is_file():
            return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent
    msg = f"docker-compose.yml not found (searched from {start})"
    raise ComposeError(msg)


def _ensure_docker() -> None:
    """Verify that the ``docker`` CLI is available and the daemon is running."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
    except FileNotFoundError as exc:
        raise ComposeError(
            "Docker is not installed. Install from https://docs.docker.com/get-docker/"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ComposeError("Docker daemon is not responding (timed out after 10s).") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        if "Cannot connect" in stderr or "Is the docker daemon running" in stderr:
            raise ComposeError(
                "Docker daemon is not running. Start Docker Desktop or run: sudo systemctl start docker"
            )
        raise ComposeError(f"Docker error: {stderr}")


def _validate_profiles(profiles: list[str]) -> list[str]:
    """Return validated profile list, raising on unknown names."""
    invalid = set(profiles) - VALID_PROFILES
    if invalid:
        raise ComposeError(
            f"Unknown profile(s): {', '.join(sorted(invalid))}. "
            f"Valid profiles: {', '.join(sorted(VALID_PROFILES))}"
        )
    # "all" expands to every profile except "test"
    if "all" in profiles:
        return sorted(VALID_PROFILES - {"all", "test"})
    return profiles


class ComposeRunner:
    """Thin wrapper around ``docker compose`` CLI.

    Parameters
    ----------
    project_dir:
        Directory containing (or ancestor of) ``docker-compose.yml``.
        Defaults to the current working directory.
    """

    def __init__(self, project_dir: Path | None = None) -> None:
        self.compose_file = _find_compose_file(project_dir)
        self.project_dir = self.compose_file.parent

    # -- helpers ---------------------------------------------------------------

    def _base_cmd(self) -> list[str]:
        return ["docker", "compose", "-f", str(self.compose_file)]

    def _profile_args(self, profiles: list[str] | None) -> list[str]:
        resolved = _validate_profiles(profiles or list(DEFAULT_PROFILES))
        args: list[str] = []
        for p in resolved:
            args.extend(["--profile", p])
        return args

    # -- public API ------------------------------------------------------------

    def run(
        self,
        *args: str,
        profiles: list[str] | None = None,
        capture: bool = False,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Execute a ``docker compose`` sub-command.

        Parameters
        ----------
        *args:
            Arguments passed after ``docker compose``, e.g. ``"up", "-d"``.
        profiles:
            Compose profiles to activate.  Defaults to :data:`DEFAULT_PROFILES`.
        capture:
            If *True* capture stdout/stderr; otherwise inherit the terminal.
        timeout:
            Optional timeout in seconds.
        """
        _ensure_docker()
        cmd = self._base_cmd() + self._profile_args(profiles) + list(args)
        logger.debug("compose command: %s", " ".join(cmd))
        try:
            return subprocess.run(
                cmd,
                capture_output=capture,
                timeout=timeout,
                cwd=str(self.project_dir),
            )
        except subprocess.TimeoutExpired as exc:
            raise ComposeError(f"Command timed out after {timeout}s") from exc
        except FileNotFoundError as exc:
            raise ComposeError(
                "Docker is not installed. Install from https://docs.docker.com/get-docker/"
            ) from exc

    def run_attached(
        self,
        *args: str,
        profiles: list[str] | None = None,
    ) -> int:
        """Run a ``docker compose`` command in the foreground with signal forwarding.

        Returns the child process exit code.
        """
        _ensure_docker()
        cmd = self._base_cmd() + self._profile_args(profiles) + list(args)
        logger.debug("compose attached: %s", " ".join(cmd))

        proc = subprocess.Popen(
            cmd,
            cwd=str(self.project_dir),
            # Create new process group so we can forward signals
            preexec_fn=os.setsid if os.name != "nt" else None,
        )

        original_sigint = signal.getsignal(signal.SIGINT)
        original_sigterm = signal.getsignal(signal.SIGTERM)

        def _forward(signum: int, frame: object) -> None:  # noqa: ARG001
            """Forward signal to the compose process group."""
            import contextlib

            with contextlib.suppress(OSError, ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signum)

        signal.signal(signal.SIGINT, _forward)
        signal.signal(signal.SIGTERM, _forward)
        try:
            return proc.wait()
        finally:
            signal.signal(signal.SIGINT, original_sigint)
            signal.signal(signal.SIGTERM, original_sigterm)

    def ps(self, profiles: list[str] | None = None) -> list[dict[str, str]]:
        """Return a list of service status dicts via ``docker compose ps --format json``."""
        import json

        result = self.run(
            "ps",
            "--format",
            "json",
            "-a",
            profiles=profiles,
            capture=True,
            timeout=15,
        )
        stdout = result.stdout.decode(errors="replace").strip()
        if not stdout:
            return []
        # docker compose ps --format json outputs one JSON object per line
        services: list[dict[str, str]] = []
        for line in stdout.splitlines():
            line = line.strip()
            if line:
                try:
                    services.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return services
