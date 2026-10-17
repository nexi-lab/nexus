#!/usr/bin/env python3
"""Preserve merge history and reject edits outside explicit conflict resolutions."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys


def git(*arguments: str) -> bytes:
    return subprocess.check_output(["git", *arguments], stderr=subprocess.PIPE)


def check(base: str, head: str) -> list[str]:
    base = git("rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}").decode().strip()
    head = git("rev-parse", "--verify", "--end-of-options", f"{head}^{{commit}}").decode().strip()
    git("merge-base", base, head)
    errors = []
    for commit in git("rev-list", "--merges", f"{base}..{head}").decode().splitlines():
        parents = git("show", "-s", "--format=%P", commit).decode().split()
        if len(parents) != 2:
            errors.append(f"{commit}: only two-parent merges can be verified")
            continue
        result = subprocess.run(
            ["git", "merge-tree", "--write-tree", "--name-only", "--no-messages", "-z", *parents],
            capture_output=True,
        )
        if result.returncode not in (0, 1):
            raise RuntimeError(result.stderr.decode(errors="replace"))
        tree, *conflicts = result.stdout.rstrip(b"\0").split(b"\0")
        if not tree:
            raise RuntimeError(f"{commit}: Git did not produce a merge tree")
        changed = git("diff", "--no-renames", "--name-only", "-z", tree.decode(), commit)
        unexpected = set(changed.rstrip(b"\0").split(b"\0")) - set(conflicts) - {b""}
        if unexpected:
            paths = ", ".join(repr(path.decode(errors="replace")) for path in sorted(unexpected))
            errors.append(
                f"{commit}: unexpected merge edits in {paths}; put these in a separate commit"
            )
        for path in conflicts:
            # Git's exact parent labels distinguish unresolved merge output from
            # ordinary documentation that describes conflict-marker syntax.
            content = subprocess.run(
                ["git", "show", f"{commit}:{os.fsdecode(path)}"], capture_output=True
            )
            if content.returncode == 0 and (
                f"<<<<<<< {parents[0]}".encode() in content.stdout
                or f">>>>>>> {parents[1]}".encode() in content.stdout
            ):
                errors.append(f"{commit}: unresolved conflict in {os.fsdecode(path)!r}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args()
    try:
        errors = check(args.base, args.head)
    except (subprocess.CalledProcessError, RuntimeError) as error:
        print(f"Cannot verify merge history: {error}", file=sys.stderr)
        return 1
    for error in errors:
        print(error, file=sys.stderr)
    if not errors:
        print("Merge history verified; no unrelated edits in merge commits")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
