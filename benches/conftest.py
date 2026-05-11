"""Bench-suite conftest: makes `src/` importable for benches run outside `tests/`."""

import sys
from pathlib import Path

_src = Path(__file__).parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))
