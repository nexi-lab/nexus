"""Benchmark tests for identifying Python hotspots for Rust acceleration.

Run all benchmarks:
    pytest tests/benchmarks/ -v --benchmark-only --benchmark-sort=mean

Run specific category:
    pytest tests/benchmarks/test_core_operations.py::TestHashingBenchmarks -v --benchmark-only

See issue #570 for context.
"""
