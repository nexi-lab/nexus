"""Tests for project type detector."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nexus.sandbox.sandbox_provider import CodeExecutionResult
from nexus.validation.detector import detect_project_validators


def _mock_provider(ls_stdout: str, exit_code: int = 0) -> AsyncMock:
    provider = AsyncMock()
    provider.run_code = AsyncMock(
        return_value=CodeExecutionResult(
            stdout=ls_stdout,
            stderr="",
            exit_code=exit_code,
            execution_time=0.05,
        )
    )
    return provider


class TestDetectProjectValidators:
    @pytest.mark.asyncio
    async def test_python_project(self):
        provider = _mock_provider("pyproject.toml\nsrc\ntests\n")
        result = await detect_project_validators("sb1", provider)
        assert "ruff" in result
        assert "mypy" in result

    @pytest.mark.asyncio
    async def test_javascript_project(self):
        provider = _mock_provider("package.json\nnode_modules\nsrc\n")
        result = await detect_project_validators("sb1", provider)
        assert "eslint" in result
        assert "ruff" not in result

    @pytest.mark.asyncio
    async def test_rust_project(self):
        provider = _mock_provider("Cargo.toml\nsrc\n")
        result = await detect_project_validators("sb1", provider)
        assert "cargo-clippy" in result

    @pytest.mark.asyncio
    async def test_go_project(self):
        """go.mod detected but no go-vet validator yet."""
        provider = _mock_provider("go.mod\nmain.go\n")
        result = await detect_project_validators("sb1", provider)
        # No go-vet in DETECTION_RULES yet
        assert result == []

    @pytest.mark.asyncio
    async def test_multi_language_monorepo(self):
        provider = _mock_provider("pyproject.toml\npackage.json\nREADME.md\n")
        result = await detect_project_validators("sb1", provider)
        assert "ruff" in result
        assert "mypy" in result
        assert "eslint" in result

    @pytest.mark.asyncio
    async def test_empty_workspace(self):
        provider = _mock_provider("")
        result = await detect_project_validators("sb1", provider)
        assert result == []

    @pytest.mark.asyncio
    async def test_no_matching_files(self):
        provider = _mock_provider("README.md\nLICENSE\n.gitignore\n")
        result = await detect_project_validators("sb1", provider)
        assert result == []

    @pytest.mark.asyncio
    async def test_setup_py_without_pyproject(self):
        provider = _mock_provider("setup.py\nsrc\n")
        result = await detect_project_validators("sb1", provider)
        assert "ruff" in result
        assert "mypy" in result

    @pytest.mark.asyncio
    async def test_ruff_toml_detection(self):
        provider = _mock_provider("ruff.toml\nmain.py\n")
        result = await detect_project_validators("sb1", provider)
        assert "ruff" in result

    @pytest.mark.asyncio
    async def test_eslintrc_json_detection(self):
        provider = _mock_provider(".eslintrc.json\nindex.js\n")
        result = await detect_project_validators("sb1", provider)
        assert "eslint" in result

    @pytest.mark.asyncio
    async def test_ls_failure(self):
        provider = _mock_provider("", exit_code=1)
        result = await detect_project_validators("sb1", provider)
        assert result == []

    @pytest.mark.asyncio
    async def test_provider_exception(self):
        provider = AsyncMock()
        provider.run_code = AsyncMock(side_effect=RuntimeError("sandbox down"))
        result = await detect_project_validators("sb1", provider)
        assert result == []

    @pytest.mark.asyncio
    async def test_no_duplicate_validators(self):
        """pyproject.toml and ruff.toml both trigger ruff — should appear once."""
        provider = _mock_provider("pyproject.toml\nruff.toml\n")
        result = await detect_project_validators("sb1", provider)
        assert result.count("ruff") == 1
