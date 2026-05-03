#!/usr/bin/env python3
"""Compatibility shim for crates that reject modern libprotoc versions.

protobuf-build 0.14 accepts only 3.x in its PROTOC preflight check, but recent
protobuf releases report calendar-style versions such as 31.1. Cargo config
points PROTOC at this script so dependency build scripts see an acceptable
version before this workspace's build.rs files run.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path


def _platform_package() -> str | None:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin":
        if machine == "arm64":
            return "protoc-bin-vendored-macos-aarch_64"
        return "protoc-bin-vendored-macos-x86_64"
    if system == "linux":
        if machine in {"x86_64", "amd64"}:
            return "protoc-bin-vendored-linux-x86_64"
        if machine in {"aarch64", "arm64"}:
            return "protoc-bin-vendored-linux-aarch_64"
        if machine in {"i386", "i686", "x86"}:
            return "protoc-bin-vendored-linux-x86_32"
        if machine in {"ppc64le", "powerpc64le"}:
            return "protoc-bin-vendored-linux-ppcle_64"
        if machine == "s390x":
            return "protoc-bin-vendored-linux-s390_64"
    if system == "windows":
        return "protoc-bin-vendored-win32"
    return None


def _find_vendored_protoc() -> str | None:
    package = _platform_package()
    if package is None:
        return None

    cargo_home = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo"))
    registry_src = cargo_home / "registry" / "src"
    candidates = sorted(registry_src.glob(f"*/{package}-*/bin/protoc*"), reverse=True)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _real_protoc() -> str:
    override = os.environ.get("NEXUS_REAL_PROTOC")
    if override:
        return override

    system_protoc = shutil.which("protoc")
    if system_protoc:
        return system_protoc

    vendored = _find_vendored_protoc()
    if vendored:
        return vendored

    raise SystemExit(
        "Unable to locate protoc. Install protobuf-compiler, set NEXUS_REAL_PROTOC, "
        "or run cargo fetch so protoc-bin-vendored is available."
    )


def main() -> int:
    if sys.argv[1:] == ["--version"]:
        print("libprotoc 3.21.12")
        return 0

    real_protoc = _real_protoc()
    os.execv(real_protoc, [real_protoc, *sys.argv[1:]])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
