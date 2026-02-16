"""Astraea-style scheduling policies (Issue #1274).

Pure-function policy modules for the state-aware MLFQ scheduler:
- classifier: Request → PriorityClass mapping with runtime adjustments
- hrrn: Highest Response Ratio Next scoring and ranking
- fair_share: Per-agent admission control and concurrency limiting
"""
