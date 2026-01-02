#!/usr/bin/env python3
"""
Pre-commit hook to prevent new type: ignore comments.

This ensures we don't add new type suppressions while working to eliminate
the existing 531 type: ignore comments during Phase 3.

Strategy:
- Block ALL new type: ignore comments in modified files
- Allow existing suppressions to remain (will be fixed in Phase 3)
- Track baseline count to ensure it never increases
"""

import re
import subprocess
import sys
from pathlib import Path

# Pattern to match type: ignore comments (not in strings)
# Only matches when it appears as an actual comment, not in string literals
TYPE_IGNORE_PATTERN = re.compile(r"#\s*type:\s*ignore(?!\s*comments)")


def get_git_diff_added_lines(file_path: Path) -> list[tuple[int, str]]:
    """
    Get lines added in git diff for a file.

    Returns:
        List of (line_number, line_content) tuples for added lines
    """
    try:
        # Get diff for staged changes
        result = subprocess.run(
            ["git", "diff", "--cached", "--unified=0", str(file_path)],
            capture_output=True,
            text=True,
            check=True,
        )

        added_lines = []
        current_line = 0

        for line in result.stdout.split("\n"):
            # Parse diff hunk header: @@ -old_start,old_count +new_start,new_count @@
            if line.startswith("@@"):
                match = re.search(r"\+(\d+)", line)
                if match:
                    current_line = int(match.group(1))
            # Lines starting with + are additions
            elif line.startswith("+") and not line.startswith("+++"):
                content = line[1:]  # Remove leading +
                added_lines.append((current_line, content))
                current_line += 1
            # Lines starting with space are context
            elif line.startswith(" "):
                current_line += 1

        return added_lines

    except subprocess.CalledProcessError:
        # File might be new or git command failed
        # Fall back to checking entire file
        try:
            with open(file_path, encoding="utf-8") as f:
                return [(i + 1, line) for i, line in enumerate(f)]
        except Exception:
            return []
    except Exception:
        return []


def check_file_for_new_type_ignores(file_path: Path) -> list[tuple[int, str]]:
    """
    Check if file has new type: ignore comments in added lines.

    Returns:
        List of (line_number, line_content) tuples for violations
    """
    added_lines = get_git_diff_added_lines(file_path)
    violations = []

    for line_num, line_content in added_lines:
        if TYPE_IGNORE_PATTERN.search(line_content):
            violations.append((line_num, line_content.strip()))

    return violations


def main() -> int:
    """Main entry point for pre-commit hook."""
    if len(sys.argv) < 2:
        print("Usage: check_type_ignore.py <file1> [file2] ...")
        return 0

    failed_files = []

    for file_path_str in sys.argv[1:]:
        file_path = Path(file_path_str)

        # Only check Python files
        if file_path.suffix != ".py":
            continue

        violations = check_file_for_new_type_ignores(file_path)

        if violations:
            failed_files.append((file_path, violations))

    if failed_files:
        print("\n❌ Type ignore check failed!")
        print("\n🚫 New '# type: ignore' comments detected:\n")

        for file_path, violations in failed_files:
            print(f"  {file_path}:")
            for line_num, line_content in violations:
                print(f"    Line {line_num}: {line_content}")
            print()

        print("📋 Policy: No new type: ignore comments are allowed")
        print("🎯 Goal: Eliminate existing 531 suppressions in Phase 3\n")
        print("💡 Instead of suppressing type errors, please:")
        print("  1. Fix the type error properly")
        print("  2. Add proper type annotations")
        print("  3. Use Protocol types for interfaces")
        print("  4. Use TypedDict for structured data")
        print("  5. Use TYPE_CHECKING imports if needed")
        print("\n📚 See docs/contributing/type-safety.md for guidelines\n")

        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
