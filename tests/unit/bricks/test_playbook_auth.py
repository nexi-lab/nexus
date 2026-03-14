"""Regression tests for playbook delete authorization — Issue #2960 H9.

Verifies that delete_playbook checks _check_permission before deleting.

Note: Uses source inspection to verify the security check is present,
avoiding import chain issues with missing optional dependencies (nats).
"""

import ast
from pathlib import Path

import pytest


def _get_playbook_source() -> str:
    """Read playbook.py source from the worktree."""
    src_path = (
        Path(__file__).resolve().parents[3] / "src" / "nexus" / "services" / "ace" / "playbook.py"
    )
    return src_path.read_text()


class TestPlaybookDeleteAuthorization:
    """Regression: H9 — missing authorization on delete_playbook."""

    def test_delete_playbook_has_permission_check(self) -> None:
        """delete_playbook must call _check_permission before deleting."""
        source = _get_playbook_source()
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "delete_playbook":
                method_source = ast.get_source_segment(source, node)
                assert method_source is not None

                assert "_check_permission" in method_source, (
                    "delete_playbook must call _check_permission. "
                    "This is a regression of Issue #2960 H9."
                )
                assert "PermissionError" in method_source, (
                    "delete_playbook must raise PermissionError on unauthorized access."
                )
                return

        pytest.fail("delete_playbook method not found in playbook.py")

    def test_delete_playbook_checks_before_delete(self) -> None:
        """The permission check must occur BEFORE session.delete()."""
        source = _get_playbook_source()
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "delete_playbook":
                method_source = ast.get_source_segment(source, node)
                assert method_source is not None

                perm_idx = method_source.find("_check_permission")
                delete_idx = method_source.find("session.delete")
                assert perm_idx < delete_idx, (
                    "_check_permission must be called before session.delete. "
                    "Order matters: check first, then delete."
                )
                return

        pytest.fail("delete_playbook method not found in playbook.py")

    def test_all_mutating_methods_have_permission_checks(self) -> None:
        """All mutating methods (update, delete) should have permission checks."""
        source = _get_playbook_source()
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name != "PlaybookManager":
                continue

            for method in node.body:
                if not isinstance(method, ast.FunctionDef):
                    continue
                if method.name.startswith("_") or method.name.startswith("get_"):
                    continue
                if method.name in ("delete_playbook", "update_playbook"):
                    method_source = ast.get_source_segment(source, method)
                    assert method_source is not None
                    assert "_check_permission" in method_source, (
                        f"Mutating method '{method.name}' must call _check_permission."
                    )
