from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "test_build_perf_e2e.py"
SPEC = importlib.util.spec_from_file_location("test_build_perf_e2e_script", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_plan_auth_restore_requires_the_complete_original_line() -> None:
    assert MODULE._plan_auth_line_restored("# Plan\n- Configure authentication\n")
    assert not MODULE._plan_auth_line_restored("# Plan\nConfigure authentication\n")
    assert not MODULE._plan_auth_line_restored(
        "# Plan\n- Configure authentication\n- Configure auth (test-edit)\n"
    )
