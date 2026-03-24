#!/usr/bin/env python3
"""Analyze per-test coverage contexts to find redundant tests.

Reads a coverage.json (produced with --cov-context=test) and identifies
pairs/groups of tests whose covered line-sets overlap heavily, suggesting
one test may be redundant.

Usage:
    pytest tests/unit/ --cov=nexus --cov-context=test --cov-report=json
    python scripts/find_redundant_tests.py coverage.json

Environment variables:
    OVERLAP_THRESHOLD  Jaccard similarity threshold (0.0–1.0, default 0.95)
    TOP_N              Max number of redundant pairs to report (default 50)
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path


def load_coverage(path: str) -> dict[str, set[str]]:
    """Load coverage.json and return {test_name: set_of_covered_lines}.

    Each "covered line" is encoded as "filename:lineno" so overlap
    comparison is global, not per-file.
    """
    with open(path) as f:
        data = json.load(f)

    # coverage.py with contexts stores data under "files" → filename →
    # "contexts" → { "lineno": ["ctx1", "ctx2", ...] }
    # We invert this to: test_name → set of "file:line" strings.
    test_lines: dict[str, set[str]] = defaultdict(set)

    files = data.get("files", {})
    for filename, file_data in files.items():
        contexts = file_data.get("contexts", {})
        for lineno, ctx_list in contexts.items():
            loc = f"{filename}:{lineno}"
            for ctx in ctx_list:
                # Skip the empty/global context
                if ctx and ctx != "":
                    test_lines[ctx].add(loc)

    return dict(test_lines)


def jaccard(a: set, b: set) -> float:
    """Jaccard similarity between two sets."""
    if not a and not b:
        return 1.0
    union = len(a | b)
    if union == 0:
        return 0.0
    return len(a & b) / union


def subset_ratio(a: set, b: set) -> float:
    """What fraction of a's lines are also covered by b?"""
    if not a:
        return 1.0
    return len(a & b) / len(a)


def find_redundant_pairs(
    test_lines: dict[str, set[str]],
    threshold: float = 0.95,
    top_n: int = 50,
) -> list[dict]:
    """Find test pairs with Jaccard similarity >= threshold.

    Returns list of dicts sorted by similarity descending.
    """
    tests = list(test_lines.keys())
    # Filter out tests with very few lines (likely trivial/parameterized)
    tests = [t for t in tests if len(test_lines[t]) >= 3]

    pairs: list[dict] = []

    # O(n^2) but test counts per module are manageable after collection
    for i in range(len(tests)):
        for j in range(i + 1, len(tests)):
            a, b = tests[i], tests[j]
            sim = jaccard(test_lines[a], test_lines[b])
            if sim >= threshold:
                pairs.append(
                    {
                        "test_a": a,
                        "test_b": b,
                        "jaccard": round(sim, 4),
                        "lines_a": len(test_lines[a]),
                        "lines_b": len(test_lines[b]),
                        "shared": len(test_lines[a] & test_lines[b]),
                    }
                )

    pairs.sort(key=lambda p: p["jaccard"], reverse=True)
    return pairs[:top_n]


def find_subset_tests(
    test_lines: dict[str, set[str]],
    threshold: float = 0.95,
    top_n: int = 50,
) -> list[dict]:
    """Find tests whose coverage is a near-subset of another test.

    If test A covers 95%+ of the same lines as test B, and B covers
    significantly more, A may be redundant (B is strictly stronger).
    """
    tests = [t for t in test_lines if len(test_lines[t]) >= 3]
    subsets: list[dict] = []

    for i in range(len(tests)):
        for j in range(len(tests)):
            if i == j:
                continue
            a, b = tests[i], tests[j]
            ratio = subset_ratio(test_lines[a], test_lines[b])
            if ratio >= threshold and len(test_lines[b]) > len(test_lines[a]):
                subsets.append(
                    {
                        "subset_test": a,
                        "superset_test": b,
                        "subset_ratio": round(ratio, 4),
                        "lines_subset": len(test_lines[a]),
                        "lines_superset": len(test_lines[b]),
                    }
                )

    subsets.sort(key=lambda s: s["subset_ratio"], reverse=True)
    return subsets[:top_n]


def main():
    if len(sys.argv) < 2:
        print("Usage: python find_redundant_tests.py <coverage.json>")
        sys.exit(1)

    cov_path = sys.argv[1]
    if not Path(cov_path).exists():
        print(f"Error: {cov_path} not found")
        sys.exit(1)

    threshold = float(os.environ.get("OVERLAP_THRESHOLD", "0.95"))
    top_n = int(os.environ.get("TOP_N", "50"))

    print(f"Loading coverage data from {cov_path}...")
    test_lines = load_coverage(cov_path)
    print(f"Found {len(test_lines)} test contexts\n")

    if not test_lines:
        print("No test contexts found. Make sure you ran pytest with --cov-context=test")
        sys.exit(0)

    # --- Redundant pairs (high Jaccard) ---
    print(f"=== Redundant Test Pairs (Jaccard >= {threshold}) ===\n")
    pairs = find_redundant_pairs(test_lines, threshold=threshold, top_n=top_n)
    if pairs:
        for p in pairs:
            print(f"  Jaccard={p['jaccard']:.2f}  shared={p['shared']} lines")
            print(f"    A: {p['test_a']} ({p['lines_a']} lines)")
            print(f"    B: {p['test_b']} ({p['lines_b']} lines)")
            print()
        print(f"Total redundant pairs: {len(pairs)}\n")
    else:
        print("  No redundant pairs found.\n")

    # --- Subset tests (one test is strictly weaker than another) ---
    print(f"=== Subset Tests (coverage {threshold:.0%}+ contained in another) ===\n")
    subsets = find_subset_tests(test_lines, threshold=threshold, top_n=top_n)
    if subsets:
        for s in subsets:
            print(f"  Subset ratio={s['subset_ratio']:.2f}")
            print(f"    WEAK:   {s['subset_test']} ({s['lines_subset']} lines)")
            print(f"    STRONG: {s['superset_test']} ({s['lines_superset']} lines)")
            print()
        print(f"Total subset relationships: {len(subsets)}\n")
    else:
        print("  No subset tests found.\n")

    # --- Summary ---
    total_flagged = len(
        {p["test_a"] for p in pairs}
        | {p["test_b"] for p in pairs}
        | {s["subset_test"] for s in subsets}
    )
    print("=== Summary ===")
    print(f"Total tests analyzed: {len(test_lines)}")
    print(f"Redundant pairs: {len(pairs)}")
    print(f"Subset relationships: {len(subsets)}")
    print(f"Unique tests flagged: {total_flagged}")
    print(f"Estimated prunable: {len({s['subset_test'] for s in subsets})}")


if __name__ == "__main__":
    main()
