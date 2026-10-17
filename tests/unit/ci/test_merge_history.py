"""Exercise the CI guard against actual Git trees, including resolved conflicts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_merge_history.py"


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True, check=check
    )


def write(repo: Path, path: str, content: str) -> None:
    (repo / path).write_text(content, encoding="utf8")


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "--all")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-b", "base")
    git(tmp_path, "config", "user.name", "History fixture")
    git(tmp_path, "config", "user.email", "history@example.invalid")
    git(tmp_path, "config", "commit.gpgsign", "false")
    git(tmp_path, "config", "core.autocrlf", "false")
    git(tmp_path, "config", "core.hooksPath", str(tmp_path / "empty-hooks"))
    write(tmp_path, "shared.txt", "initial\n")
    commit(tmp_path, "initial")
    return tmp_path


def guard(repo: Path, base: str = "base", head: str = "HEAD") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--base", base, "--head", head],
        cwd=repo,
        text=True,
        capture_output=True,
    )


def diverge(repo: Path, *, conflict: bool = False) -> None:
    git(repo, "switch", "-c", "feature")
    write(repo, "shared.txt" if conflict else "feature file.txt", "feature\n")
    commit(repo, "feature change")
    git(repo, "switch", "base")
    write(repo, "shared.txt" if conflict else "base.txt", "base change\n")
    commit(repo, "base change")
    git(repo, "switch", "feature")


def test_linear_history(repo: Path) -> None:
    git(repo, "switch", "-c", "feature")
    write(repo, "feature.txt", "feature\n")
    commit(repo, "feature")
    assert guard(repo).returncode == 0


def test_clean_merge_keeps_both_histories(repo: Path) -> None:
    diverge(repo)
    git(repo, "merge", "--no-ff", "base", "-m", "merge base")
    assert guard(repo).returncode == 0


def test_clean_merge_cannot_hide_an_extra_change(repo: Path) -> None:
    diverge(repo)
    git(repo, "merge", "--no-commit", "--no-ff", "base")
    write(repo, "base.txt", "silently dropped base change\n")
    commit(repo, "merge with unexpected edit")
    result = guard(repo)
    assert result.returncode == 1
    assert "unexpected merge edits" in result.stderr
    assert "base.txt" in result.stderr


def test_manual_conflict_resolution_is_preserved(repo: Path) -> None:
    diverge(repo, conflict=True)
    assert git(repo, "merge", "--no-commit", "--no-ff", "base", check=False).returncode == 1
    write(repo, "shared.txt", "feature and base reconciled\n")
    commit(repo, "resolve conflict")
    assert guard(repo).returncode == 0


def test_conflict_resolution_cannot_hide_unrelated_edits(repo: Path) -> None:
    diverge(repo, conflict=True)
    git(repo, "merge", "--no-commit", "--no-ff", "base", check=False)
    write(repo, "shared.txt", "resolved\n")
    write(repo, "unrelated.txt", "unexpected\n")
    commit(repo, "resolution plus unrelated change")
    result = guard(repo)
    assert result.returncode == 1
    assert "unrelated.txt" in result.stderr


def test_unresolved_conflict_is_rejected(repo: Path) -> None:
    diverge(repo, conflict=True)
    parents = [git(repo, "rev-parse", ref).stdout.strip() for ref in ("feature", "base")]
    tree = git(repo, "merge-tree", "--write-tree", *parents, check=False).stdout.splitlines()[0]
    unresolved = git(
        repo, "commit-tree", tree, "-p", parents[0], "-p", parents[1], "-m", "unresolved"
    ).stdout.strip()
    result = guard(repo, head=unresolved)
    assert result.returncode == 1
    assert "unresolved conflict" in result.stderr


def test_missing_base_fails_closed(repo: Path) -> None:
    result = guard(repo, base="nonexistent")
    assert result.returncode == 1
    assert "Cannot verify merge history" in result.stderr
