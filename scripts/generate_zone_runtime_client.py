#!/usr/bin/env python3
"""Generate the Python client for the exact-pinned nexus-vfs ZoneRuntime proto."""

from __future__ import annotations

import re
import sys
import tempfile
import urllib.request
from pathlib import Path

from grpc_tools import protoc

ROOT = Path(__file__).resolve().parents[1]
PROTO_PATH = "proto/nexus/grpc/vfs/zone_runtime.proto"
RAW_BASE = "https://raw.githubusercontent.com/nexi-lab/nexus-vfs"
OUTPUTS = (
    Path("src/nexus/grpc/vfs/zone_runtime_pb2.py"),
    Path("src/nexus/grpc/vfs/zone_runtime_pb2_grpc.py"),
)


def pinned_rev() -> str:
    revisions = set(
        re.findall(
            r'git\s*=\s*"https://github\.com/nexi-lab/nexus-vfs"\s*,\s*rev\s*=\s*"([0-9a-f]{40})"',
            (ROOT / "Cargo.toml").read_text(encoding="utf-8"),
        )
    )
    if len(revisions) != 1:
        raise SystemExit(f"expected one workspace nexus-vfs pin, found {sorted(revisions)}")
    return revisions.pop()


def main() -> int:
    check = "--check" in sys.argv
    revision = pinned_rev()
    url = f"{RAW_BASE}/{revision}/{PROTO_PATH}"
    with urllib.request.urlopen(url, timeout=60) as response:
        source = response.read()

    with tempfile.TemporaryDirectory(prefix="nexus-zone-runtime-proto-") as directory:
        root = Path(directory)
        proto = root / PROTO_PATH
        proto.parent.mkdir(parents=True)
        proto.write_bytes(source)
        (root / "generated").mkdir()
        result = protoc.main(
            [
                "grpc_tools.protoc",
                f"-I{root / 'proto'}",
                f"--python_out={root / 'generated'}",
                f"--grpc_python_out={root / 'generated'}",
                str(proto),
            ]
        )
        if result != 0:
            raise SystemExit(result)

        stale: list[str] = []
        for destination in OUTPUTS:
            generated = root / "generated" / destination.relative_to("src")
            data = generated.read_bytes()
            target = ROOT / destination
            if target.exists() and target.read_bytes() == data:
                continue
            stale.append(destination.as_posix())
            if not check:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)

    if check and stale:
        print("stale generated zone-runtime client: " + ", ".join(stale), file=sys.stderr)
        return 1
    print("zone-runtime client up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
