"""Index generator for `extensions.json`.

Builds a deterministic JSON snapshot of all in-tree manifests at install/CI
time. The result is consumed by `ManifestStore.load_json_index` for fast,
zero-import enumeration.

Usage:
    python -m nexus.extensions.index build [--output PATH]
    python -m nexus.extensions.index verify [--against PATH]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nexus.extensions.manifest import AnyManifest
from nexus.extensions.store import INDEX_SCHEMA_VERSION


def _canonicalize(value: Any) -> Any:
    """Recursively normalize a JSON-serializable value for cross-process determinism.

    Pydantic dumps frozensets in hash iteration order (PYTHONHASHSEED-dependent),
    which makes the generated index drift between machines. We sort any list of
    primitive values so frozenset-derived fields (e.g. ConnectorManifest.capabilities)
    serialize stably without needing per-field custom serializers.
    """
    if isinstance(value, dict):
        return {k: _canonicalize(v) for k, v in value.items()}
    if isinstance(value, list):
        canon = [_canonicalize(v) for v in value]
        if all(isinstance(v, (str, int, float, bool)) or v is None for v in canon):
            return sorted(canon, key=lambda x: (x is None, str(x)))
        return canon
    return value


def _serialize(manifests: Iterable[AnyManifest], frozen_time: str | None) -> str:
    sorted_manifests = sorted(
        (_canonicalize(m.model_dump(mode="json")) for m in manifests),
        key=lambda d: (d["kind"], d["name"]),
    )
    payload = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "generated_at": frozen_time
        or _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "manifests": sorted_manifests,
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def build_index(
    *,
    manifests: Iterable[AnyManifest],
    output_path: Path,
    frozen_time: str | None = None,
) -> None:
    """Write a deterministic JSON index to `output_path`.

    `frozen_time` is for testing — pass a fixed string to make output bit-stable.
    """
    output_path.write_text(_serialize(manifests, frozen_time=frozen_time))


@dataclass(frozen=True)
class VerifyResult:
    is_clean: bool
    diff: str | None


def verify_index(
    *,
    manifests: Iterable[AnyManifest],
    expected_path: Path,
    frozen_time: str | None = None,
) -> VerifyResult:
    """Compare the generated index against the on-disk file.

    `generated_at` is read from the on-disk file and reused when regenerating,
    so verify only flags structural drift — not clock differences.
    """
    on_disk = expected_path.read_text() if expected_path.exists() else ""

    if frozen_time is None and on_disk:
        try:
            existing = json.loads(on_disk)
            frozen_time = existing.get("generated_at")
        except json.JSONDecodeError:
            frozen_time = None

    fresh = _serialize(manifests, frozen_time=frozen_time)

    if fresh == on_disk:
        return VerifyResult(is_clean=True, diff=None)

    diff = "".join(
        difflib.unified_diff(
            on_disk.splitlines(keepends=True),
            fresh.splitlines(keepends=True),
            fromfile=str(expected_path),
            tofile="<generated>",
        )
    )
    return VerifyResult(is_clean=False, diff=diff)


# Source-tree subdir <-> manifest kind contract. Used by both the index
# build (strict — reject mismatches so they don't end up in the wheel) and
# the runtime filesystem fallback (warn and skip — sibling isolation).
_SUBDIR_TO_KIND: tuple[tuple[str, str], ...] = (
    ("backends/connectors", "connector"),
    ("bricks", "brick"),
    ("plugins", "plugin"),
)


def _discover_in_tree_manifests() -> list[AnyManifest]:
    """Walk the source tree for `_manifest.py` files and return their MANIFEST.

    Strict mode: collisions on ``(kind, name)`` across files raise so a stray
    duplicate in-tree manifest fails the build/verify hook instead of silently
    being dropped on first-wins precedence. Path/kind mismatch (e.g. a
    ``backends/connectors/foo/_manifest.py`` that exposes a PluginManifest)
    also raises so a wheel cannot ship a manifest under the wrong subtree
    and become invisible to its own kind's introspection.
    """
    from nexus.extensions.store import _load_manifest_module

    repo_root = Path(__file__).parent.parent  # src/nexus/
    by_key: dict[tuple[str, str], tuple[AnyManifest, Path]] = {}
    duplicates: list[str] = []
    kind_mismatches: list[str] = []

    for subdir, expected_kind in _SUBDIR_TO_KIND:
        root = repo_root / subdir
        if not root.exists():
            continue
        for child in sorted(root.iterdir()):
            manifest_file = child / "_manifest.py"
            if not child.is_dir() or not manifest_file.exists():
                continue
            manifest = _load_manifest_module(manifest_file, strict=True)
            if manifest is None:  # pragma: no cover — strict raises instead
                continue
            if manifest.kind != expected_kind:
                kind_mismatches.append(
                    f"{manifest_file} declares kind={manifest.kind!r} "
                    f"but lives under {subdir}/ (expected {expected_kind!r})"
                )
                continue
            key = (manifest.kind, manifest.name)
            if key in by_key:
                _, prev = by_key[key]
                duplicates.append(f"{key[0]}/{key[1]} declared in {prev} and {manifest_file}")
                continue
            by_key[key] = (manifest, manifest_file)

    if kind_mismatches:
        raise RuntimeError("in-tree manifest kind mismatch: " + "; ".join(sorted(kind_mismatches)))
    if duplicates:
        raise RuntimeError("duplicate in-tree manifests: " + "; ".join(sorted(duplicates)))

    return [m for m, _ in by_key.values()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nexus-extensions-index")
    sub = parser.add_subparsers(dest="cmd", required=True)

    build_p = sub.add_parser("build", help="Generate extensions.json")
    build_p.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).parent / "_index" / "extensions.json",
    )

    verify_p = sub.add_parser("verify", help="Check extensions.json is up to date")
    verify_p.add_argument(
        "--against",
        type=Path,
        default=Path(__file__).parent / "_index" / "extensions.json",
    )

    args = parser.parse_args(argv)
    manifests = _discover_in_tree_manifests()

    if args.cmd == "build":
        build_index(manifests=manifests, output_path=args.output)
        print(f"Wrote {len(manifests)} manifests to {args.output}")
        return 0

    if args.cmd == "verify":
        result = verify_index(manifests=manifests, expected_path=args.against)
        if result.is_clean:
            print(f"OK: {args.against} is up to date")
            return 0
        print(f"DRIFT: {args.against} differs from generated output")
        if result.diff:
            print(result.diff)
        return 1

    return 2


def _cli_entry() -> None:
    """Console-script wrapper — propagates main()'s return code as exit status."""
    sys.exit(main())


if __name__ == "__main__":
    _cli_entry()
