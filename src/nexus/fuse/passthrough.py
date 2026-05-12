"""Python orchestration helpers for Rust-owned FUSE passthrough mounts."""

from __future__ import annotations

import os
import platform
import subprocess
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nexus.fuse.rust_client import RustFUSEClient

_STARTUP_TIMEOUT_SECS = 5.0
_STARTUP_POLL_INTERVAL_SECS = 0.05
_STARTUP_LOG_BYTES = 4096
_PASSTHROUGH_HOOK_OPERATIONS = (
    "read",
    "write",
    "write_batch",
    "delete",
    "rename",
    "copy",
    "mkdir",
    "rmdir",
    "stat",
    "access",
    "open",
)
_PASSTHROUGH_ENV_PREFIX = "NEXUS_FUSE_PASSTHROUGH"


def _parse_bool(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_patterns(value: str | None) -> list[str]:
    if not value:
        return []
    return [segment.strip() for segment in value.split(",") if segment.strip()]


@dataclass(slots=True)
class PassthroughOptions:
    enabled: bool = False
    patterns: list[str] = field(default_factory=list)
    deny_patterns: list[str] = field(default_factory=list)
    threshold_bytes: int = 128 * 1024
    require: bool = False
    backing_dir: Path | None = None

    @classmethod
    def from_env(cls) -> PassthroughOptions:
        backing_dir = os.environ.get("NEXUS_FUSE_PASSTHROUGH_BACKING_DIR")
        return cls(
            enabled=_parse_bool(os.environ.get("NEXUS_FUSE_PASSTHROUGH")),
            patterns=_parse_patterns(os.environ.get("NEXUS_FUSE_PASSTHROUGH_PATTERNS")),
            deny_patterns=_parse_patterns(os.environ.get("NEXUS_FUSE_PASSTHROUGH_DENY_PATTERNS")),
            threshold_bytes=int(
                os.environ.get("NEXUS_FUSE_PASSTHROUGH_THRESHOLD_BYTES", str(128 * 1024))
            ),
            require=_parse_bool(os.environ.get("NEXUS_FUSE_PASSTHROUGH_REQUIRE")),
            backing_dir=Path(backing_dir) if backing_dir else None,
        )


def mount_is_passthrough_safe(nexus_fs: Any, *, mode_value: str, context: Any | None) -> bool:
    if platform.system() != "Linux":
        return False
    if context is not None:
        return False
    if mode_value != "binary":
        return False

    try:
        kernel = getattr(nexus_fs, "_kernel", None)
        if kernel is None:
            return False
        hook_count = kernel.hook_count
    except Exception:
        return False

    if not callable(hook_count):
        return False

    try:
        if any(hook_count(operation) != 0 for operation in _PASSTHROUGH_HOOK_OPERATIONS):
            return False
        if int(nexus_fs.mount_hook_count) != 0:
            return False
        if int(nexus_fs.unmount_hook_count) != 0:
            return False
    except Exception:
        return False

    return True


@dataclass(slots=True)
class RustPassthroughMount:
    rust_binary: str
    nexus_url: str
    api_key: str
    mount_point: Path
    options: PassthroughOptions
    agent_id: str | None = None
    process: subprocess.Popen[str] | None = None
    api_key_file: Path | None = None
    log_file: Path | None = None

    @classmethod
    def create(
        cls,
        *,
        nexus_url: str,
        api_key: str,
        mount_point: Path,
        options: PassthroughOptions,
        agent_id: str | None = None,
    ) -> RustPassthroughMount:
        finder = RustFUSEClient.__new__(RustFUSEClient)
        finder.daemon_process = None
        finder.socket_path = None
        finder.sock = None
        rust_binary = finder._find_rust_binary()
        return cls(
            rust_binary=rust_binary,
            nexus_url=nexus_url,
            api_key=api_key,
            mount_point=mount_point,
            options=options,
            agent_id=agent_id,
        )

    def build_command(self) -> tuple[list[str], dict[str, str], Path]:
        self._remove_api_key_file()
        api_key_file = self._write_api_key_file()

        cmd = [
            self.rust_binary,
            "mount",
            str(self.mount_point),
            "--url",
            self.nexus_url,
            "--api-key-file",
            str(api_key_file),
            "--foreground",
            "--passthrough",
            "--passthrough-threshold-bytes",
            str(self.options.threshold_bytes),
        ]

        for pattern in self.options.patterns:
            cmd.extend(["--passthrough-pattern", pattern])
        for pattern in self.options.deny_patterns:
            cmd.extend(["--passthrough-deny-pattern", pattern])
        if self.options.require:
            cmd.append("--passthrough-require")
        if self.options.backing_dir is not None:
            cmd.extend(["--passthrough-backing-dir", str(self.options.backing_dir)])
        if self.agent_id:
            cmd.extend(["--agent-id", self.agent_id])

        env = dict(os.environ)
        for key in list(env):
            if key == "NEXUS_API_KEY" or key.startswith(_PASSTHROUGH_ENV_PREFIX):
                env.pop(key, None)
        return cmd, env, api_key_file

    def start(self) -> None:
        if self.process is not None:
            if self.process.poll() is None:
                raise RuntimeError("Rust passthrough mount is already running")
            self.process = None
            self._remove_api_key_file()

        cmd, env, _api_key_file = self.build_command()
        log_handle = None
        try:
            log_file = self._create_log_file()
            log_handle = log_file.open("w", encoding="utf-8")
            self.process = subprocess.Popen(
                cmd,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )
            log_handle.close()
            log_handle = None
            self._wait_until_mounted()
        except Exception:
            if log_handle is not None:
                log_handle.close()
            if self.process is not None and self.process.poll() is None:
                self.stop()
            else:
                self.process = None
                self._remove_api_key_file()
                self._remove_log_file()
            raise

    def stop(self) -> None:
        process = self.process
        try:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        finally:
            self.process = None
            self._remove_api_key_file()
            self._remove_log_file()

    def _wait_until_mounted(self) -> None:
        deadline = time.monotonic() + _STARTUP_TIMEOUT_SECS
        while True:
            if self.mount_point.is_mount():
                return

            process = self.process
            if process is None:
                raise RuntimeError("Rust passthrough mount process was not started")

            exit_code = process.poll()
            if exit_code is not None:
                details = self._startup_log_details()
                raise RuntimeError(
                    f"Rust passthrough mount exited before mounting "
                    f"(exit code {exit_code}){details}"
                )

            if time.monotonic() >= deadline:
                details = self._startup_log_details()
                raise RuntimeError(
                    "Rust passthrough mount did not become ready within "
                    f"{_STARTUP_TIMEOUT_SECS:.1f}s{details}"
                )

            time.sleep(_STARTUP_POLL_INTERVAL_SECS)

    def _write_api_key_file(self) -> Path:
        fd, name = tempfile.mkstemp(prefix="nexus-fuse-api-key-", text=True)
        api_key_file = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(self.api_key)
            api_key_file.chmod(0o600)
        except BaseException:
            with suppress(FileNotFoundError):
                api_key_file.unlink()
            raise

        self.api_key_file = api_key_file
        return api_key_file

    def _remove_api_key_file(self) -> None:
        if self.api_key_file is None:
            return
        with suppress(FileNotFoundError):
            self.api_key_file.unlink()
        self.api_key_file = None

    def _create_log_file(self) -> Path:
        self._remove_log_file()
        fd, name = tempfile.mkstemp(prefix="nexus-fuse-passthrough-", suffix=".log", text=True)
        os.close(fd)
        self.log_file = Path(name)
        return self.log_file

    def _startup_log_details(self) -> str:
        if self.log_file is None:
            return ""
        with suppress(OSError, UnicodeDecodeError):
            output = self.log_file.read_text(encoding="utf-8")[-_STARTUP_LOG_BYTES:].strip()
            if output:
                return f": {output}"
        return ""

    def _remove_log_file(self) -> None:
        if self.log_file is None:
            return
        with suppress(FileNotFoundError):
            self.log_file.unlink()
        self.log_file = None
