"""Read the shared nexus-vfs build revision and verify Cargo's resolution."""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

VFS_REPOSITORY = "https://github.com/nexi-lab/nexus-vfs"


def resolve_revision(manifest: Path) -> str:
    with manifest.open("rb") as stream:
        workspace = tomllib.load(stream)["workspace"]
    revisions = {
        dependency.get("rev", "")
        for dependency in workspace["dependencies"].values()
        if isinstance(dependency, dict) and dependency.get("git") == VFS_REPOSITORY
    }
    if len(revisions) != 1:
        raise ValueError("workspace nexus-vfs dependencies must share one revision")
    revision = revisions.pop()
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("nexus-vfs revision must be a full lowercase commit hash")

    with manifest.with_name("Cargo.lock").open("rb") as stream:
        packages = tomllib.load(stream)["package"]
    resolved = 0
    for package in packages:
        source = package.get("source", "")
        if not source.startswith(f"git+{VFS_REPOSITORY}?"):
            continue
        resolved += 1
        source_url = urlsplit(source)
        if parse_qs(source_url.query).get("rev") != [revision] or source_url.fragment != revision:
            raise ValueError("Cargo.lock nexus-vfs resolution differs from the workspace revision")
    if not resolved:
        raise ValueError("Cargo.lock must resolve at least one nexus-vfs package")
    return revision


def main() -> int:
    if len(sys.argv) > 2:
        print("usage: nexus_vfs_revision.py [Cargo.toml]", file=sys.stderr)
        return 1
    manifest = Path(sys.argv[1]) if len(sys.argv) == 2 else Path("Cargo.toml")
    try:
        revision = resolve_revision(manifest)
    except (OSError, KeyError, TypeError, ValueError) as error:
        print(f"invalid nexus-vfs build revision: {error}", file=sys.stderr)
        return 1
    print(revision)
    return 0


if __name__ == "__main__":
    sys.exit(main())
