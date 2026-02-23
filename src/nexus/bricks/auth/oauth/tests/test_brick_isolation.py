"""Tests that the OAuth brick has zero imports from nexus.server, nexus.core, nexus.bricks.rebac."""

import ast
import pathlib

import pytest

BRICK_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Modules that are part of the brick core (must be isolation-clean)
BRICK_CORE_MODULES = [
    "types.py",
    "protocol.py",
    "base_provider.py",
    "pending.py",
    "providers/google.py",
    "providers/microsoft.py",
    "providers/x.py",
]

FORBIDDEN_PREFIXES = ("nexus.server", "nexus.core", "nexus.bricks.rebac")


def _get_imports(filepath: pathlib.Path) -> list[str]:
    """Extract all import module names from a Python file."""
    source = filepath.read_text()
    tree = ast.parse(source, filename=str(filepath))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


@pytest.mark.parametrize("module_path", BRICK_CORE_MODULES)
def test_brick_core_module_isolation(module_path: str) -> None:
    filepath = BRICK_ROOT / module_path
    if not filepath.exists():
        pytest.skip(f"{module_path} not yet created")

    imports = _get_imports(filepath)
    violations = [
        imp for imp in imports if any(imp.startswith(prefix) for prefix in FORBIDDEN_PREFIXES)
    ]
    assert violations == [], (
        f"Brick isolation violation in {module_path}: {violations}. "
        f"Brick core must not import from {FORBIDDEN_PREFIXES}"
    )
