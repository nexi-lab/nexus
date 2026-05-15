"""Validate that all code examples in docs are correct.

Uses pytest-examples to discover, lint, and run code blocks
from Markdown files. Backend-dependent examples (S3, GCS, Drive)
are linted for syntax only — they run in integration CI with
real credentials.
"""

from __future__ import annotations

import pytest
from pytest_examples import CodeExample, EvalExample, find_examples

# Examples that need cloud credentials — lint only, don't execute
_BACKEND_EXAMPLES = {"backends/s3.md", "backends/gcs.md", "backends/gdrive.md"}


def _is_backend_example(example: CodeExample) -> bool:
    """Check if a code example comes from a backend-specific doc page."""
    path_str = str(example.path)
    return any(path_str.endswith(be) for be in _BACKEND_EXAMPLES)


@pytest.mark.parametrize("example", find_examples("docs/"), ids=str)
def test_docs_examples(example: CodeExample, eval_example: EvalExample) -> None:
    if example.lang != "python":
        pytest.skip("not a Python example")

    # All examples get linted for syntax correctness
    eval_example.lint(example)

    # Backend-dependent examples are lint-only in standard CI
    if _is_backend_example(example):
        pytest.skip("backend example — runs in integration CI")

    # Local-only examples are fully executed
    if "# skip-test" not in example.source:
        eval_example.run(example)
