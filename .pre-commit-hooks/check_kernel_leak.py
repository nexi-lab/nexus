#!/usr/bin/env python3
"""Pre-commit hook: block NexusFS._kernel access from service tier.

Service-tier code (server/, bricks/) must interact with the kernel
exclusively through public syscall methods (sys_read, sys_write,
sys_stat, etc.).  Reaching into fs._kernel or nx._kernel bypasses
the gRPC boundary and breaks the kernel contract.

Allowed callers:
- factory/ — DI wiring passes nx._kernel to service constructors
- core/    — kernel implementation owns _kernel
- fs/      — kernel adapter layer
- tests/   — test fixtures may mock _kernel
- cli/     — comments referencing _kernel (not calls)
"""

import re
import sys
from pathlib import Path

# Directories where ._kernel access on an *external* object is forbidden.
# self._kernel = own attribute is always OK (e.g. FederationRPC stores
# its own kernel ref as self._kernel — that's DI, not boundary leak).
GUARDED_DIRS = (
    Path("src") / "nexus" / "server",
    Path("src") / "nexus" / "bricks",
)

# Pattern: something._kernel that is NOT self._kernel
# Matches: fs._kernel, nx._kernel, nexus_fs._kernel, svc.nexus_fs._kernel
# Skips:   self._kernel (own attribute, OK)
_LEAK_RE = re.compile(r"(?<!self)\._kernel\b")

# Skip comment-only lines and suppressed lines
_COMMENT_RE = re.compile(r"^\s*#")
_SUPPRESS_RE = re.compile(r"#\s*kernel-leak-ok\b")


def _check_file(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return errors
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _COMMENT_RE.match(line):
            continue
        if _SUPPRESS_RE.search(line):
            continue
        if _LEAK_RE.search(line):
            errors.append(f"{path}:{lineno}: {line.strip()}")
    return errors


def main() -> int:
    errors: list[str] = []
    for guarded_dir in GUARDED_DIRS:
        if not guarded_dir.exists():
            continue
        for py_file in guarded_dir.rglob("*.py"):
            # Skip test files inside bricks/ — use as_posix() so the
            # "/tests/" match works on Windows (native paths use "\").
            if "/tests/" in py_file.as_posix():
                continue
            errors.extend(_check_file(py_file))
    if errors:
        print("ERROR: NexusFS._kernel accessed from service tier.")
        print("Use public fs.sys_*() methods instead.\n")
        for e in errors:
            print(f"  {e}")
        print(f"\n{len(errors)} violation(s) found.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
