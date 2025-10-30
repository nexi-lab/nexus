#!/usr/bin/env python3
"""Test version consistency between pyproject.toml and __init__.py.

This test ensures that the version string in pyproject.toml matches
the __version__ variable in the package __init__.py file.

This prevents issues where:
- Package is published with wrong version number
- CLI reports wrong version
- Version drift between source and metadata
"""

import sys
import tomllib
from pathlib import Path


def get_pyproject_version() -> str:
    """Extract version from pyproject.toml.

    Returns:
        Version string from [project] section

    Raises:
        FileNotFoundError: If pyproject.toml not found
        KeyError: If version not found in pyproject.toml
    """
    # Find project root (parent of tests/ directory)
    project_root = Path(__file__).parent.parent.parent
    pyproject_path = project_root / "pyproject.toml"

    if not pyproject_path.exists():
        raise FileNotFoundError(f"pyproject.toml not found at {pyproject_path}")

    with open(pyproject_path, "rb") as f:
        pyproject = tomllib.load(f)

    try:
        return pyproject["project"]["version"]
    except KeyError as e:
        raise KeyError("Version not found in pyproject.toml [project] section") from e


def get_package_version() -> str:
    """Extract version from package __init__.py.

    Returns:
        Version string from __version__ variable

    Raises:
        ImportError: If package cannot be imported
        AttributeError: If __version__ not found
    """
    # Add src directory to path to import package
    project_root = Path(__file__).parent.parent.parent
    src_path = project_root / "src"

    if src_path not in map(Path, sys.path):
        sys.path.insert(0, str(src_path))

    try:
        import nexus

        return nexus.__version__
    except ImportError as e:
        raise ImportError(f"Cannot import nexus package: {e}") from e
    except AttributeError as e:
        raise AttributeError("__version__ not found in nexus package") from e


def test_version_consistency() -> None:
    """Test that pyproject.toml version matches package __version__.

    This test fails if:
    - pyproject.toml version != nexus.__version__
    - Either file is missing or malformed
    """
    pyproject_version = get_pyproject_version()
    package_version = get_package_version()

    assert pyproject_version == package_version, (
        f"Version mismatch!\n"
        f"  pyproject.toml: {pyproject_version}\n"
        f"  nexus.__version__: {package_version}\n"
        f"\n"
        f"Please update one of:\n"
        f"  - pyproject.toml [project] version\n"
        f"  - src/nexus/__init__.py __version__"
    )


if __name__ == "__main__":
    # Allow running as standalone script
    try:
        test_version_consistency()
        print("✓ Version consistency check passed")
    except AssertionError as e:
        print(f"✗ Version consistency check failed:\n{e}")
        sys.exit(1)
    except Exception as e:
        print(f"✗ Error during version check: {e}")
        sys.exit(1)
