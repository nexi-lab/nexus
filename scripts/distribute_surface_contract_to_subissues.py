#!/usr/bin/env python3
"""Append the surface-coverage contract appendix to all 21 subissue bodies.

Reads the issue list, calls `gh issue edit` per issue.
Idempotent — re-runs replace the prior appendix in-place via sentinel match.

Run AFTER #4161 PR merges to develop, not as part of the PR itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `from scripts...` imports when running this file directly via
# `uv run python scripts/distribute_surface_contract_to_subissues.py`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402
from collections import defaultdict  # noqa: E402

from scripts.surface_coverage.distribute import apply_appendix, build_appendix  # noqa: E402
from scripts.surface_coverage.schema import load_yaml  # noqa: E402

# All subissues to amend (epics + children)
_TARGET_ISSUES: tuple[int, ...] = (
    4119,
    4120,
    4121,
    4139,  # epics
    4122,
    4123,
    4124,
    4125,  # lite children
    4126,
    4127,
    4128,
    4129,
    4130,
    4131,  # sandbox children
    4132,
    4133,
    4134,
    4135,
    4136,
    4137,
    4138,  # full children
)

_REPO = "nexi-lab/nexus"


def _gh_get_body(issue: int) -> str:
    out = subprocess.check_output(
        ["gh", "issue", "view", str(issue), "--repo", _REPO, "--json", "body"],
        text=True,
    )
    return json.loads(out)["body"]


def _gh_set_body(issue: int, body: str, *, dry_run: bool) -> None:
    if dry_run:
        print(f"[dry-run] would edit #{issue} ({len(body)} chars)")
        return
    subprocess.run(
        ["gh", "issue", "edit", str(issue), "--repo", _REPO, "--body-file", "-"],
        input=body,
        text=True,
        check=True,
    )
    print(f"updated #{issue}")


def _owners_from_yaml(yaml_path: Path) -> dict[int, list[str]]:
    coverage = load_yaml(yaml_path)
    out: dict[int, list[str]] = defaultdict(list)
    for op in coverage.operations:
        if op.owning_issue is not None:
            out[op.owning_issue].append(op.id)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--yaml",
        type=Path,
        default=Path("docs/surface-coverage/api-rpc-surface-coverage.yaml"),
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--only",
        type=int,
        action="append",
        help="restrict to specific issue number(s) (testing)",
    )
    args = p.parse_args(argv)

    owners = _owners_from_yaml(args.yaml)
    targets = args.only if args.only else _TARGET_ISSUES
    for issue in targets:
        body = _gh_get_body(issue)
        appendix = build_appendix(issue_number=issue, owned_op_ids=owners.get(issue, []))
        new_body = apply_appendix(body, appendix)
        if new_body == body:
            print(f"#{issue}: no change")
            continue
        _gh_set_body(issue, new_body, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
