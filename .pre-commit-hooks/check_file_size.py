#!/usr/bin/env python3
"""
Pre-commit hook to enforce maximum file size limits.

This prevents new large files from being committed and encourages
breaking down monolithic files into smaller, more maintainable modules.

Configuration:
- MAX_LINES: Maximum allowed lines per Python file (default: 1000)
- EXCEPTIONS: Files that are temporarily exempt (should be empty after refactoring)
"""

import sys
from pathlib import Path

MAX_LINES = 2000

# Temporary exceptions - these files should be split during refactoring
# This list should shrink to zero as Phase 2 progresses
EXCEPTIONS = [
    "src/nexus/core/nexus_fs.py",  # 6,167 lines - Phase 2 refactoring
    "src/nexus/core/nexus_fs_core.py",  # 2,807 lines - Phase 2 refactoring
    "src/nexus/core/nexus_fs_search.py",  # 2,175 lines - Phase 2 refactoring
    "src/nexus/core/nexus_fs_rebac.py",  # 2,554 lines - Phase 2 refactoring
    "src/nexus/core/nexus_fs_mounts.py",  # 2,048 lines - Phase 2 refactoring
    "src/nexus/core/nexus_fs_oauth.py",  # 1,116 lines - Phase 2 refactoring
    "src/nexus/core/nexus_fs_skills.py",  # 874 lines - Phase 2 refactoring
    "src/nexus/services/permissions/rebac_manager.py",  # 4,400 lines - Phase 2 consolidation
    "src/nexus/services/permissions/rebac_manager_enhanced.py",  # 4,500 lines - Phase 2 consolidation
    "src/nexus/services/permissions/tiger_cache.py",  # 2,896 lines - Leopard-style directory grants
    "src/nexus/services/permissions/nexus_fs_rebac.py",  # 2,192 lines - NexusFS mixin
    "src/nexus/services/rebac_service.py",  # 2,400 lines - sync + async methods for ReBAC delegation
    "src/nexus/remote/client.py",  # 5,000 lines - Phase 4 splitting
    "src/nexus/remote/async_client.py",  # 2,500 lines - Phase 4 splitting
    "src/nexus/storage/models/__init__.py",  # 3,400 lines - Phase 4 splitting (partially done)
    "src/nexus/server/fastapi_server.py",  # 2,133 lines - Phase 4 splitting
    "src/nexus/services/memory/memory_api.py",  # 3,012 lines - moved from core/, split tracked
]


def count_lines(file_path: Path) -> int:
    """Count non-empty lines in a file."""
    try:
        with open(file_path, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except Exception as e:
        print(f"Warning: Could not read {file_path}: {e}")
        return 0


def check_file_size(file_path: Path) -> tuple[bool, int]:
    """
    Check if file exceeds maximum line count.

    Returns:
        (passes_check, line_count)
    """
    # Normalize path for comparison with exceptions
    normalized_path = str(file_path).replace("\\", "/")

    # Check if file is in exceptions list
    if any(normalized_path.endswith(exception) for exception in EXCEPTIONS):
        return True, 0  # Pass check, don't count

    line_count = count_lines(file_path)
    passes = line_count <= MAX_LINES

    return passes, line_count


def main() -> int:
    """Main entry point for pre-commit hook."""
    if len(sys.argv) < 2:
        print("Usage: check_file_size.py <file1> [file2] ...")
        return 0

    failed_files = []

    for file_path_str in sys.argv[1:]:
        file_path = Path(file_path_str)

        # Only check Python files
        if file_path.suffix != ".py":
            continue

        # Skip test files (tests can be longer)
        if "tests/" in str(file_path) or "test_" in file_path.name:
            continue

        passes, line_count = check_file_size(file_path)

        if not passes:
            failed_files.append((file_path, line_count))

    if failed_files:
        print("\n❌ File size check failed!")
        print(f"\nThe following files exceed the {MAX_LINES} line limit:\n")

        for file_path, line_count in failed_files:
            print(f"  {file_path}: {line_count} lines (exceeds by {line_count - MAX_LINES})")

        print(f"\n📏 Standard: Python files should not exceed {MAX_LINES} lines")
        print("💡 Tip: Break large files into smaller, focused modules")
        print("\nIf this is a legacy file being refactored:")
        print("  1. Add it to EXCEPTIONS in .pre-commit-hooks/check_file_size.py")
        print("  2. Create a GitHub issue to track splitting the file")
        print("  3. Remove from EXCEPTIONS once split\n")

        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
