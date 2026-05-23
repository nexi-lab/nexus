"""Extract DeploymentProfile enum values via AST."""

from pathlib import Path

import pytest

from scripts.surface_coverage.extract_profiles import extract_profile_names


def test_extract_profiles_from_fixture(tmp_path: Path):
    """Test extraction from a fixture enum."""
    f = tmp_path / "deployment_profile.py"
    f.write_text(
        "from enum import Enum\n"
        "\n"
        "class DeploymentProfile(str, Enum):\n"
        '    LITE = "lite"\n'
        '    SANDBOX = "sandbox"\n'
        '    FULL = "full"\n'
        '    REMOTE = "remote"\n'
    )
    results = extract_profile_names(f, enum_class="DeploymentProfile")
    assert set(results) >= {"lite", "sandbox", "full"}


def test_extract_profiles_real_file_smoke(repo_root: Path):
    """Smoke test: ensure real file has expected profiles."""
    real = repo_root / "src/nexus/contracts/deployment_profile.py"
    if not real.exists():
        pytest.skip("Real file not found")
    results = extract_profile_names(real, enum_class="DeploymentProfile")
    assert "lite" in results or "LITE" in results or len(results) >= 3
