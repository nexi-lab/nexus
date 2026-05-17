#!/usr/bin/env python3
"""Render the surface-coverage YAML into HTML."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scripts.surface_coverage.render import render_html
from scripts.surface_coverage.schema import load_yaml


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
