#!/usr/bin/env python3
"""Render the surface-coverage YAML into HTML."""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `from scripts...` imports when running this file directly via
# `uv run python scripts/render_api_surface_coverage.py`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse  # noqa: E402

from scripts.surface_coverage.render import render_html  # noqa: E402
from scripts.surface_coverage.schema import load_yaml  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--input",
        type=Path,
        default=Path("docs/architecture/api-rpc-surface-coverage.yaml"),
    )
    p.add_argument(
        "--output",
        type=Path,
        default=Path("docs/architecture/api-rpc-surface-coverage.html"),
    )
    args = p.parse_args(argv)
    coverage = load_yaml(args.input)
    args.output.write_text(render_html(coverage))
    return 0


if __name__ == "__main__":
    sys.exit(main())
