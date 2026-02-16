"""AST-based import cycle detection (Issue #1291, Decision 9A).

Parses all ``src/nexus/**/*.py`` files, extracts top-level import statements
(skipping TYPE_CHECKING blocks and function-body deferred imports), builds a
directed graph, and asserts the graph is acyclic (topological sort succeeds).
"""

from __future__ import annotations

import ast
import os
from collections import defaultdict
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "nexus"


def _is_inside_type_checking(node: ast.AST, tree: ast.Module) -> bool:
    """Check if an import node is inside an ``if TYPE_CHECKING:`` block."""
    for top_node in ast.walk(tree):
        if not isinstance(top_node, ast.If):
            continue
        # Check if the test is `TYPE_CHECKING` (bare name or attribute)
        test = top_node.test
        is_tc = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        )
        if not is_tc:
            continue
        # Check if our node is inside this if-block
        for child in ast.walk(top_node):
            if child is node:
                return True
    return False


def _is_inside_function(node: ast.AST, tree: ast.Module) -> bool:
    """Check if an import node is inside a function or method body."""
    for top_node in ast.walk(tree):
        if not isinstance(top_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(top_node):
            if child is node:
                return True
    return False


def _extract_imports(filepath: Path) -> list[str]:
    """Extract runtime nexus.* imports from a Python file.

    Skips:
    - Imports inside ``if TYPE_CHECKING:`` blocks
    - Imports inside function/method bodies (deferred = intentional)
    """
    try:
        source = filepath.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(filepath))
    except (SyntaxError, UnicodeDecodeError):
        return []

    imports: list[str] = []

    for node in ast.iter_child_nodes(tree):
        # Only look at top-level statements (not nested in functions/classes)
        if isinstance(node, ast.Import):
            if _is_inside_type_checking(node, tree):
                continue
            for alias in node.names:
                if alias.name.startswith("nexus"):
                    # Convert to module path: nexus.core.permissions -> nexus.core.permissions
                    imports.append(alias.name)

        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("nexus"):
                if _is_inside_type_checking(node, tree):
                    continue
                imports.append(node.module)

        elif isinstance(node, ast.ClassDef):
            # Check class-level imports (but not method-level)
            for class_node in ast.iter_child_nodes(node):
                if isinstance(class_node, ast.Import):
                    if _is_inside_type_checking(class_node, tree):
                        continue
                    if _is_inside_function(class_node, tree):
                        continue
                    for alias in class_node.names:
                        if alias.name.startswith("nexus"):
                            imports.append(alias.name)
                elif isinstance(class_node, ast.ImportFrom):
                    if class_node.module and class_node.module.startswith("nexus"):
                        if _is_inside_type_checking(class_node, tree):
                            continue
                        if _is_inside_function(class_node, tree):
                            continue
                        imports.append(class_node.module)

    return imports


def _filepath_to_module(filepath: Path) -> str:
    """Convert file path to module name: src/nexus/core/types.py -> nexus.core.types."""
    src_root = _SRC_ROOT.parent  # src/
    rel = filepath.relative_to(src_root)
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1].removesuffix(".py")
    return ".".join(parts)


def _build_import_graph() -> dict[str, set[str]]:
    """Build directed graph of runtime imports across all nexus modules."""
    graph: dict[str, set[str]] = defaultdict(set)

    for root, _dirs, files in os.walk(_SRC_ROOT):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            filepath = Path(root) / fname
            module_name = _filepath_to_module(filepath)
            imported_modules = _extract_imports(filepath)
            for imp in imported_modules:
                if imp != module_name:  # no self-edges
                    graph[module_name].add(imp)

    return dict(graph)


def _find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Find all cycles in a directed graph using DFS."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = defaultdict(int)
    cycles: list[list[str]] = []
    path: list[str] = []

    def dfs(node: str) -> None:
        color[node] = GRAY
        path.append(node)
        for neighbor in graph.get(node, set()):
            if color[neighbor] == GRAY:
                # Found cycle: extract it from path
                cycle_start = path.index(neighbor)
                cycle = path[cycle_start:] + [neighbor]
                cycles.append(cycle)
            elif color[neighbor] == WHITE:
                dfs(neighbor)
        path.pop()
        color[node] = BLACK

    all_nodes = set(graph.keys())
    for edges in graph.values():
        all_nodes.update(edges)

    for node in sorted(all_nodes):
        if color[node] == WHITE:
            dfs(node)

    return cycles


class TestImportCycles:
    """Verify no runtime import cycles exist in the nexus package."""

    def test_no_runtime_cycles(self) -> None:
        """AST-based DAG verification: no runtime import cycles."""
        graph = _build_import_graph()
        cycles = _find_cycles(graph)

        if cycles:
            # Format cycle info for readable error message
            cycle_strs = []
            for cycle in cycles[:5]:  # Show at most 5 cycles
                cycle_strs.append(" -> ".join(cycle))
            msg = f"Found {len(cycles)} runtime import cycle(s):\n" + "\n".join(
                f"  {i + 1}. {c}" for i, c in enumerate(cycle_strs)
            )
            pytest.fail(msg)

    def test_graph_has_entries(self) -> None:
        """Sanity check: import graph should have many nodes."""
        graph = _build_import_graph()
        assert len(graph) > 50, f"Expected 50+ modules in graph, got {len(graph)}"
