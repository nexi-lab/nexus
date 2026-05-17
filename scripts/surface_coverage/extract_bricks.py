"""Discover bricks by scanning src/nexus/bricks/*/brick_factory.py.

Each brick_factory.py module declares (as module-level constants):
    BRICK_NAME: str | None  - profile gate name (None = always on)
    TIER: str               - "independent" | "dependent"
    RESULT_KEY: str         - key in result dict
    def create(ctx, system) -> result

We parse these via AST to avoid importing the brick (which has runtime deps).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RawBrick:
    id: str  # directory name, e.g. "rebac"
    brick_name: str | None  # profile gate, e.g. "BRICK_REBAC"
    tier: str | None  # "independent" | "dependent"
    result_key: str | None
    source: str  # "path:line"


def extract_bricks(bricks_root: Path) -> list[RawBrick]:
    """Walk bricks_root, find each <brick>/brick_factory.py, extract metadata."""
    out: list[RawBrick] = []
    if not bricks_root.exists():
        return out
    for brick_dir in sorted(bricks_root.iterdir()):
        if not brick_dir.is_dir():
            continue
        factory_py = brick_dir / "brick_factory.py"
        if not factory_py.exists():
            continue
        meta = _parse_brick_factory(factory_py)
        out.append(
            RawBrick(
                id=brick_dir.name,
                brick_name=meta.get("BRICK_NAME"),
                tier=meta.get("TIER"),
                result_key=meta.get("RESULT_KEY"),
                source=f"{factory_py}:1",
            )
        )
    return out


def _parse_brick_factory(py_path: Path) -> dict[str, str | None]:
    """Return module-level constants we care about from a brick_factory.py."""
    out: dict[str, str | None] = {}
    try:
        tree = ast.parse(py_path.read_text())
    except (SyntaxError, UnicodeDecodeError):
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {
                    "BRICK_NAME",
                    "TIER",
                    "RESULT_KEY",
                }:
                    out[target.id] = _literal_value(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id in {"BRICK_NAME", "TIER", "RESULT_KEY"}
            and node.value is not None
        ):
            out[node.target.id] = _literal_value(node.value)
    return out


def _literal_value(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and (node.value is None or isinstance(node.value, str)):
        return node.value
    return None
