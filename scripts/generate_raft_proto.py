#!/usr/bin/env python3
"""Generate Python proto stubs for nexus.raft.

Runs grpc_tools.protoc to generate *_pb2.py and *_pb2_grpc.py from
the Raft proto files, then moves the output from the nested package
directory to the flat src/nexus/raft/ layout.

Post-processing: adds ``# noqa: F401`` to dependency imports that protoc
generates (e.g. ``from nexus.raft import commands_pb2``), because ruff
would otherwise remove them as "unused" — but they are required to
register the proto descriptors before the dependent file loads.

Usage:
    python scripts/generate_raft_proto.py
"""

import re
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

# Pattern: protoc generates cross-file imports like:
#   from nexus.raft import commands_pb2 as nexus_dot_raft_dot_commands__pb2
# ruff sees these as unused (F401) and removes them, breaking descriptor loading.
_PROTO_IMPORT_RE = re.compile(r"^(from nexus\.\w+ import \w+_pb2 as .+)$", re.MULTILINE)
_NOQA_COMMENT = "  # noqa: F401, E402"


def _add_noqa_to_proto_imports(filepath: Path) -> int:
    """Add ``# noqa: F401`` to protoc-generated cross-file imports."""
    content = filepath.read_text()
    count = 0

    def _replace(m: re.Match) -> str:
        nonlocal count
        line = m.group(1)
        if "# noqa" not in line:
            count += 1
            return f"{line}{_NOQA_COMMENT}"
        return line

    new_content = _PROTO_IMPORT_RE.sub(_replace, content)
    if count:
        filepath.write_text(new_content)
    return count


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

    # Post-process: protect cross-file proto imports from ruff F401 removal
    for pb_file in OUT_DIR.glob("*_pb2.py"):
        n = _add_noqa_to_proto_imports(pb_file)
        if n:
            print(f"  Patched {pb_file.name}: added # noqa: F401 to {n} import(s)")

    shutil.rmtree(TMP_DIR, ignore_errors=True)
    print("Done.")


if __name__ == "__main__":
    main()
