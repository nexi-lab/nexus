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
extract_brick_name = _mod.extract_brick_name
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
            from nexus.contracts.protocols.search import SearchProtocol
            from nexus.storage.record_store import RecordStoreABC
            import os
        """)
        assert check_file(path) == []

    def test_detects_from_nexus_core_import(self, brick_file):
        path = brick_file("""\
            from nexus.core.nexus_fs_dispatch import DispatchMixin
        """)
        violations = check_file(path)
        assert len(violations) == 1
        assert violations[0][0] == 1  # line number
        assert "nexus.core" in violations[0][2]  # description

    def test_detects_import_nexus_core(self, brick_file):
        path = brick_file("""\
            import nexus.core.nexus_fs_dispatch
        """)
        violations = check_file(path)
        assert len(violations) == 1
        assert violations[0][0] == 1

    def test_detects_from_nexus_services_import(self, brick_file):
        path = brick_file("""\
            from nexus.services.search.search_service import SearchService
        """)
        violations = check_file(path)
        assert len(violations) == 1
        assert "nexus.services" in violations[0][2]

    def test_detects_import_nexus_services(self, brick_file):
        path = brick_file("""\
            import nexus.services.rebac.rebac_service
        """)
        violations = check_file(path)
        assert len(violations) == 1

    def test_allows_nexus_core_protocols(self, brick_file):
        path = brick_file("""\
            from nexus.core.protocols import VFSRouterProtocol
            from nexus.core.protocols.vfs_router import VFSRouterProtocol
        """)
        assert check_file(path) == []

    def test_allows_nexus_contracts_protocols(self, brick_file):
        path = brick_file("""\
            from nexus.contracts.protocols import SearchProtocol
            from nexus.contracts.protocols.search import SearchProtocol
        """)
        assert check_file(path) == []

    def test_forbids_nexus_services_protocols(self, brick_file):
        """After protocols moved to contracts/, services.protocols is forbidden."""
        path = brick_file("""\
            from nexus.services.protocols import SearchProtocol
        """)
        violations = check_file(path)
        assert len(violations) == 1
        assert "nexus.services" in violations[0][2]

    def test_allows_nexus_storage(self, brick_file):
        path = brick_file("""\
            from nexus.storage.record_store import RecordStoreABC
            import nexus.storage
        """)
        assert check_file(path) == []

    def test_skips_comments(self, brick_file):
        path = brick_file("""\
            # from nexus.core.nexus_fs_dispatch import DispatchMixin
            # import nexus.services.search.search_service
        """)
        assert check_file(path) == []

    def test_skips_string_literals(self, brick_file):
        path = brick_file("""\
            "from nexus.core.nexus_fs_dispatch import DispatchMixin"
            'import nexus.services.search.search_service'
        """)
        assert check_file(path) == []

    def test_skips_empty_lines(self, brick_file):
        path = brick_file("""\

        """)
        assert check_file(path) == []

    def test_multiple_violations(self, brick_file):
        path = brick_file("""\
            from nexus.core.nexus_fs_dispatch import DispatchMixin
            from nexus.core.protocols.vfs_router import VFSRouterProtocol
            from nexus.services.search.search_service import SearchService
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
            from nexus.core.nexus_fs_dispatch import DispatchMixin
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
        (bricks / "bad.py").write_text("from nexus.core.nexus_fs_dispatch import DispatchMixin\n")
        monkeypatch.setattr("sys.argv", ["check_brick_imports.py"])
        monkeypatch.chdir(tmp_path)
        assert main() == 1

    def test_precommit_mode_filters_to_bricks(self, monkeypatch, tmp_path: Path):
        """Pre-commit mode only checks files with /bricks/ in path."""
        bricks = tmp_path / "src" / "nexus" / "bricks"
        bricks.mkdir(parents=True)
        bad_file = bricks / "bad.py"
        bad_file.write_text("from nexus.core.nexus_fs_dispatch import DispatchMixin\n")
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


class TestExtractBrickName:
    """Tests for extract_brick_name()."""

    def test_extracts_brick_from_path(self):
        p = Path("src/nexus/bricks/memory/service.py")
        assert extract_brick_name(p) == "memory"

    def test_extracts_brick_from_nested_path(self):
        p = Path("src/nexus/bricks/governance/approval/workflow.py")
        assert extract_brick_name(p) == "governance"

    def test_returns_none_for_non_brick_path(self):
        p = Path("src/nexus/core/nexus_fs.py")
        assert extract_brick_name(p) is None

    def test_returns_none_for_path_ending_at_bricks(self):
        p = Path("src/nexus/bricks")
        assert extract_brick_name(p) is None


class TestCrossBrickImports:
    """Tests for cross-brick import detection (Issue #2286)."""

    @pytest.fixture()
    def memory_brick_file(self, tmp_path: Path):
        """Create a file simulating a module inside bricks/memory/."""

        def _make(content: str) -> Path:
            brick_dir = tmp_path / "src" / "nexus" / "bricks" / "memory"
            brick_dir.mkdir(parents=True, exist_ok=True)
            f = brick_dir / "service.py"
            f.write_text(textwrap.dedent(content))
            return f

        return _make

    def test_detects_cross_brick_from_import(self, memory_brick_file):
        path = memory_brick_file("""\
            from nexus.bricks.pay.credits import CreditsService
        """)
        violations = check_file(path, brick_name="memory")
        assert len(violations) == 1
        assert "cross-brick import" in violations[0][2]
        assert "nexus.bricks.pay" in violations[0][2]

    def test_detects_cross_brick_import_statement(self, memory_brick_file):
        path = memory_brick_file("""\
            import nexus.bricks.pay.credits
        """)
        violations = check_file(path, brick_name="memory")
        assert len(violations) == 1
        assert "cross-brick import" in violations[0][2]

    def test_allows_same_brick_import(self, memory_brick_file):
        path = memory_brick_file("""\
            from nexus.bricks.memory.router import MemoryRouter
            import nexus.bricks.memory.enrichment
        """)
        violations = check_file(path, brick_name="memory")
        assert violations == []

    def test_detects_type_checking_cross_brick(self, memory_brick_file):
        """TYPE_CHECKING cross-brick imports are also violations."""
        path = memory_brick_file("""\
            from typing import TYPE_CHECKING
            if TYPE_CHECKING:
                from nexus.bricks.pay.protocol import ProtocolTransferRequest
        """)
        violations = check_file(path, brick_name="memory")
        assert len(violations) == 1
        assert "nexus.bricks.pay" in violations[0][2]

    def test_cross_brick_in_comment_is_skipped(self, memory_brick_file):
        path = memory_brick_file("""\
            # from nexus.bricks.pay.credits import CreditsService
        """)
        violations = check_file(path, brick_name="memory")
        assert violations == []

    def test_cross_brick_without_brick_name_not_checked(self, tmp_path: Path):
        """Files not identified as brick modules skip cross-brick checks."""
        f = tmp_path / "module.py"
        f.write_text("from nexus.bricks.search.embeddings import foo\n")
        violations = check_file(f)  # No brick_name
        assert violations == []

    def test_multiple_cross_brick_violations(self, memory_brick_file):
        path = memory_brick_file("""\
            from nexus.bricks.governance.models import GovernanceModel
            from nexus.bricks.pay.credits import CreditsService
            from nexus.bricks.memory.router import MemoryRouter
        """)
        violations = check_file(path, brick_name="memory")
        assert len(violations) == 2  # governance and pay, but not memory (same brick)
        descs = [v[2] for v in violations]
        assert any("nexus.bricks.governance" in d for d in descs)
        assert any("nexus.bricks.pay" in d for d in descs)

    def test_cross_brick_combined_with_core_violation(self, memory_brick_file):
        """Both core and cross-brick violations are reported."""
        path = memory_brick_file("""\
            from nexus.core.nexus_fs_dispatch import DispatchMixin
            from nexus.bricks.pay.credits import foo
        """)
        violations = check_file(path, brick_name="memory")
        assert len(violations) == 2
        assert "nexus.core" in violations[0][2]
        assert "cross-brick" in violations[1][2]

    def test_allowlisted_cross_brick_not_reported(self, memory_brick_file):
        """Memory->search imports in the allowlist should not be reported."""
        path = memory_brick_file("""\
            from nexus.bricks.search.embeddings import create_embedding_provider
        """)
        violations = check_file(path, brick_name="memory")
        assert violations == []


class TestCrossBrickMainIntegration:
    """Integration tests for cross-brick detection via main()."""

    def test_cross_brick_detected_in_ci_mode(self, monkeypatch, tmp_path: Path):
        bricks = tmp_path / "src" / "nexus" / "bricks" / "memory"
        bricks.mkdir(parents=True)
        (bricks / "bad.py").write_text("from nexus.bricks.pay.credits import CreditsService\n")
        monkeypatch.setattr("sys.argv", ["check_brick_imports.py"])
        monkeypatch.chdir(tmp_path)
        assert main() == 1

    def test_same_brick_import_passes(self, monkeypatch, tmp_path: Path):
        bricks = tmp_path / "src" / "nexus" / "bricks" / "memory"
        bricks.mkdir(parents=True)
        (bricks / "ok.py").write_text("from nexus.bricks.memory.router import MemoryRouter\n")
        monkeypatch.setattr("sys.argv", ["check_brick_imports.py"])
        monkeypatch.chdir(tmp_path)
        assert main() == 0
