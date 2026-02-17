"""Tests for .pre-commit-hooks/check_brick_imports.py.

Validates that the brick import checker correctly identifies forbidden imports
from nexus.core and nexus.services internals, while allowing imports from
protocol interfaces and storage ABCs.
"""

import importlib.util
import textwrap
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[3] / ".pre-commit-hooks" / "check_brick_imports.py"
_spec = importlib.util.spec_from_file_location("check_brick_imports", _SCRIPT_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

check_file = _mod.check_file
find_brick_files = _mod.find_brick_files
is_import_line = _mod.is_import_line
main = _mod.main

@pytest.fixture()
def brick_file(tmp_path: Path):
    """Helper to create a temporary Python file simulating a brick module."""

    def _make(content: str) -> Path:
        f = tmp_path / "brick_module.py"
        f.write_text(textwrap.dedent(content))
        return f

    return _make

class TestCheckFile:
    """Tests for check_file()."""

    def test_clean_file_no_violations(self, brick_file):
        path = brick_file("""\
            from nexus.core.protocols.vfs_router import VFSRouterProtocol
            from nexus.services.protocols.hook_engine import HookEngineProtocol
            from nexus.storage.record_store import RecordStoreABC
            import os
        """)
        assert check_file(path) == []

    def test_detects_from_nexus_core_import(self, brick_file):
        path = brick_file("""\
            from nexus.core.nexus_fs import NexusFS
        """)
        violations = check_file(path)
        assert len(violations) == 1
        assert violations[0][0] == 1  # line number
        assert "nexus.core" in violations[0][2]  # description

    def test_detects_import_nexus_core(self, brick_file):
        path = brick_file("""\
            import nexus.core.nexus_fs
        """)
        violations = check_file(path)
        assert len(violations) == 1
        assert violations[0][0] == 1

    def test_detects_from_nexus_services_import(self, brick_file):
        path = brick_file("""\
            from nexus.services.search_service import SearchService
        """)
        violations = check_file(path)
        assert len(violations) == 1
        assert "nexus.services" in violations[0][2]

    def test_detects_import_nexus_services(self, brick_file):
        path = brick_file("""\
            import nexus.services.rebac_service
        """)
        violations = check_file(path)
        assert len(violations) == 1

    def test_allows_nexus_core_protocols(self, brick_file):
        path = brick_file("""\
            from nexus.core.protocols import VFSRouterProtocol
            from nexus.core.protocols.vfs_router import VFSRouterProtocol
        """)
        assert check_file(path) == []

    def test_allows_nexus_services_protocols(self, brick_file):
        path = brick_file("""\
            from nexus.services.protocols import HookEngineProtocol
            from nexus.services.protocols.hook_engine import HookEngineProtocol
        """)
        assert check_file(path) == []

    def test_allows_nexus_storage(self, brick_file):
        path = brick_file("""\
            from nexus.storage.record_store import RecordStoreABC
            import nexus.storage
        """)
        assert check_file(path) == []

    def test_skips_comments(self, brick_file):
        path = brick_file("""\
            # from nexus.core.nexus_fs import NexusFS
            # import nexus.services.search_service
        """)
        assert check_file(path) == []

    def test_skips_string_literals(self, brick_file):
        path = brick_file("""\
            "from nexus.core.nexus_fs import NexusFS"
            'import nexus.services.search_service'
        """)
        assert check_file(path) == []

    def test_skips_empty_lines(self, brick_file):
        path = brick_file("""\

        """)
        assert check_file(path) == []

    def test_multiple_violations(self, brick_file):
        path = brick_file("""\
            from nexus.core.nexus_fs import NexusFS
            from nexus.core.protocols.vfs_router import VFSRouterProtocol
            from nexus.services.search_service import SearchService
            import os
        """)
        violations = check_file(path)
        assert len(violations) == 2  # line 1 and line 3
        assert violations[0][0] == 1
        assert violations[1][0] == 3

    def test_correct_line_numbers(self, brick_file):
        path = brick_file("""\
            import os
            import sys
            from nexus.core.nexus_fs import NexusFS
        """)
        violations = check_file(path)
        assert len(violations) == 1
        assert violations[0][0] == 3

    def test_empty_file(self, brick_file):
        path = brick_file("")
        assert check_file(path) == []

    def test_nonexistent_file(self, tmp_path: Path):
        """Non-existent file should not crash, just return empty."""
        fake_path = tmp_path / "nonexistent.py"
        assert check_file(fake_path) == []

    def test_catches_protocols_extra_module(self, brick_file):
        """Ensure protocols_extra (not a real protocols subpackage) is caught."""
        path = brick_file("""\
            from nexus.core.protocols_extra import Foo
        """)
        violations = check_file(path)
        assert len(violations) == 1

    def test_catches_services_protocols_extra_module(self, brick_file):
        """Ensure services.protocols_extra is caught."""
        path = brick_file("""\
            from nexus.services.protocols_extra import Bar
        """)
        violations = check_file(path)
        assert len(violations) == 1

class TestFindBrickFiles:
    """Tests for find_brick_files()."""

    def test_returns_empty_when_no_bricks_dir(self, tmp_path: Path):
        assert find_brick_files(tmp_path) == []

    def test_finds_python_files_in_bricks(self, tmp_path: Path):
        bricks = tmp_path / "src" / "nexus" / "bricks" / "my_brick"
        bricks.mkdir(parents=True)
        (bricks / "handler.py").write_text("pass")
        (bricks / "README.md").write_text("docs")
        result = find_brick_files(tmp_path)
        assert len(result) == 1
        assert result[0].name == "handler.py"

    def test_finds_nested_python_files(self, tmp_path: Path):
        bricks = tmp_path / "src" / "nexus" / "bricks"
        (bricks / "brick_a").mkdir(parents=True)
        (bricks / "brick_b").mkdir(parents=True)
        (bricks / "brick_a" / "__init__.py").write_text("")
        (bricks / "brick_b" / "__init__.py").write_text("")
        result = find_brick_files(tmp_path)
        assert len(result) == 2

class TestMain:
    """Tests for main() entry point."""

    def test_returns_zero_when_no_bricks_dir(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr("sys.argv", ["check_brick_imports.py"])
        monkeypatch.chdir(tmp_path)
        assert main() == 0

    def test_returns_zero_for_clean_bricks(self, monkeypatch, tmp_path: Path):
        bricks = tmp_path / "src" / "nexus" / "bricks"
        bricks.mkdir(parents=True)
        (bricks / "clean.py").write_text("import os\n")
        monkeypatch.setattr("sys.argv", ["check_brick_imports.py"])
        monkeypatch.chdir(tmp_path)
        assert main() == 0

    def test_returns_one_when_violations_found(self, monkeypatch, tmp_path: Path):
        bricks = tmp_path / "src" / "nexus" / "bricks"
        bricks.mkdir(parents=True)
        (bricks / "bad.py").write_text("from nexus.core.nexus_fs import NexusFS\n")
        monkeypatch.setattr("sys.argv", ["check_brick_imports.py"])
        monkeypatch.chdir(tmp_path)
        assert main() == 1

    def test_precommit_mode_filters_to_bricks(self, monkeypatch, tmp_path: Path):
        """Pre-commit mode only checks files with /bricks/ in path."""
        bricks = tmp_path / "src" / "nexus" / "bricks"
        bricks.mkdir(parents=True)
        bad_file = bricks / "bad.py"
        bad_file.write_text("from nexus.core.nexus_fs import NexusFS\n")
        # Pass a non-brick file — should be filtered out
        monkeypatch.setattr(
            "sys.argv",
            ["check_brick_imports.py", str(tmp_path / "src" / "nexus" / "core" / "foo.py")],
        )
        assert main() == 0  # Non-brick file is filtered out

class TestIsImportLine:
    """Tests for is_import_line()."""

    def test_actual_import(self):
        assert is_import_line("from nexus.core import foo") is True

    def test_comment(self):
        assert is_import_line("# from nexus.core import foo") is False

    def test_string(self):
        assert is_import_line('"from nexus.core import foo"') is False

    def test_empty(self):
        assert is_import_line("") is False

    def test_indented_comment(self):
        assert is_import_line("    # from nexus.core import foo") is False
