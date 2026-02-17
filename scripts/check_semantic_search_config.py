#!/usr/bin/env python3
"""
Check if semantic search is enabled in the config file.

Usage:
    python scripts/check_semantic_search_config.py <config_file>

Returns:
    true - if semantic_search is enabled
    false - if disabled or config file not found
"""

import sys
from pathlib import Path

import yaml


def check_semantic_search_enabled(config_file: str) -> bool:
    """
    Check if semantic search is enabled in config file.

    Args:
        config_file: Path to YAML config file

    Returns:
        True if enabled, False otherwise
    """
    try:
        if not Path(config_file).exists():
            return False

        with open(config_file) as f:
            config = yaml.safe_load(f)

        enabled = config.get("features", {}).get("semantic_search", False)
        return bool(enabled)
    except Exception as e:
        print(f"Warning: Could not read semantic_search config: {e}", file=sys.stderr)
        return False


def main() -> None:
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python check_semantic_search_config.py <config_file>", file=sys.stderr)
        sys.exit(1)

    config_file = sys.argv[1]
    enabled = check_semantic_search_enabled(config_file)

    print("true" if enabled else "false")
    sys.exit(0)


if __name__ == "__main__":
    main()
