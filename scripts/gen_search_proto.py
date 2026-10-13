"""Generate Search bindings from the exact VFS revision in Cargo's build pin.

Run with: uv run --no-project --with grpcio-tools==1.80.0 python
scripts/gen_search_proto.py --vfs-checkout /path/to/nexus-vfs [--check]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

from nexus_vfs_revision import resolve_revision

ROOT = Path(__file__).resolve().parents[1]
PROTO = Path("nexus/search/v1/search.proto")
OUTPUT = ROOT / "src/nexus/grpc/search/v1"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vfs-checkout", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if version("grpcio-tools") != "1.80.0":
        parser.error("use grpcio-tools==1.80.0 as shown in the command above")
    revision = resolve_revision(ROOT / "Cargo.toml")
    source = subprocess.check_output(
        ["git", "-C", str(args.vfs_checkout), "show", f"{revision}:rust/proto/{PROTO.as_posix()}"],
    )
    with tempfile.TemporaryDirectory(prefix="nexus-search-proto-") as staging:
        staging_root = Path(staging)
        proto_file = staging_root / PROTO
        proto_file.parent.mkdir(parents=True)
        proto_file.write_bytes(source)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "grpc_tools.protoc",
                f"-I{staging_root}",
                f"--python_out={staging_root}",
                f"--grpc_python_out={staging_root}",
                PROTO.as_posix(),
            ],
            check=True,
        )
        for name in ("search_pb2.py", "search_pb2_grpc.py"):
            generated = (proto_file.parent / name).read_text(encoding="utf8")
            generated = generated.replace(
                "from nexus.search.v1 import search_pb2",
                "from . import search_pb2",
            ).replace("'nexus.search.v1.search_pb2'", "'nexus.grpc.search.v1.search_pb2'")
            target = OUTPUT / name
            if args.check:
                if target.read_text(encoding="utf8") != generated:
                    print(f"Search binding is stale: {target.relative_to(ROOT)}", file=sys.stderr)
                    return 1
            else:
                target.write_text(generated, encoding="utf8", newline="\n")
    print(f"Search bindings {'match' if args.check else 'generated from'} VFS {revision}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
