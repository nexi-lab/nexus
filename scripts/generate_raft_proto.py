#!/usr/bin/env python3
"""Generate Python proto stubs for nexus.raft.

Runs grpc_tools.protoc to generate *_pb2.py and *_pb2_grpc.py from
the Raft proto files, then moves the output from the nested package
directory to the flat src/nexus/raft/ layout.

Usage:
    python scripts/generate_raft_proto.py
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROTO_DIR = ROOT / "proto"
OUT_DIR = ROOT / "src" / "nexus" / "raft"
TMP_DIR = OUT_DIR / "_proto_gen_tmp"

PROTO_FILES = [
    "nexus/raft/commands.proto",
    "nexus/raft/transport.proto",
]


def main() -> None:
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"-I{PROTO_DIR}",
        f"--python_out={TMP_DIR}",
        f"--grpc_python_out={TMP_DIR}",
        *PROTO_FILES,
    ]
    print(f"Running: {' '.join(cmd)}")
    subprocess.check_call(cmd)

    # protoc outputs to {TMP_DIR}/nexus/raft/ — move to flat layout
    nested = TMP_DIR / "nexus" / "raft"
    for pb_file in nested.glob("*.py"):
        dest = OUT_DIR / pb_file.name
        shutil.move(str(pb_file), str(dest))
        print(f"  {pb_file.name} → {dest}")

    shutil.rmtree(TMP_DIR, ignore_errors=True)
    print("Done.")


if __name__ == "__main__":
    main()
