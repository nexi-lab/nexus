"""Extract generic gRPC Call dispatch names from a dict OR frozenset literal via AST."""

from pathlib import Path

import pytest

from scripts.surface_coverage.extract_grpc_call import extract_grpc_call_names


def test_extract_grpc_call_from_dict_fixture(tmp_path: Path):
    f = tmp_path / "dispatch.py"
    f.write_text(
        "from typing import Callable\n"
        "def _read(req): pass\n"
        "def _write(req): pass\n"
        "\n"
        "KERNEL_SYSCALL_DISPATCH: dict[str, Callable] = {\n"
        '    "fs.read": _read,\n'
        '    "fs.write": _write,\n'
        '    "rebac.grant": _read,\n'
        "}\n"
    )
    results = extract_grpc_call_names(f, dispatch_var="KERNEL_SYSCALL_DISPATCH")
    names = {r.name for r in results}
    assert names == {"fs.read", "fs.write", "rebac.grant"}


def test_extract_grpc_call_from_frozenset_varargs_fixture(tmp_path: Path):
    f = tmp_path / "dispatch.py"
    f.write_text(
        "KERNEL_SYSCALL_NAMES: frozenset[str] = frozenset(\n"
        '    "fs.read",\n'
        '    "fs.write",\n'
        '    "rebac.grant",\n'
        ")\n"
    )
    results = extract_grpc_call_names(f, dispatch_var="KERNEL_SYSCALL_NAMES")
    names = {r.name for r in results}
    assert names == {"fs.read", "fs.write", "rebac.grant"}


def test_extract_grpc_call_from_frozenset_iterable_fixture(tmp_path: Path):
    f = tmp_path / "dispatch.py"
    f.write_text('KERNEL_SYSCALL_NAMES = frozenset([\n    "fs.read",\n    "fs.write",\n])\n')
    results = extract_grpc_call_names(f, dispatch_var="KERNEL_SYSCALL_NAMES")
    names = {r.name for r in results}
    assert names == {"fs.read", "fs.write"}


def test_extract_grpc_call_real_file_smoke(repo_root: Path):
    """Real dispatch file must yield at least one Call name via SOME var name."""
    real = repo_root / "src/nexus/server/_kernel_syscall_dispatch.py"
    if not real.exists():
        pytest.skip("dispatch file missing")
    # Real file uses KERNEL_SYSCALL_NAMES (frozenset). Other candidate names
    # tried so this still works if the codegen layout changes.
    for var in (
        "KERNEL_SYSCALL_NAMES",
        "DISPATCH",
        "_DISPATCH",
        "KERNEL_SYSCALL_DISPATCH",
        "SYSCALL_DISPATCH",
    ):
        try:
            results = extract_grpc_call_names(real, dispatch_var=var)
        except ValueError:
            continue
        if results:
            assert len(results) >= 1, "Expected at least one Call name"
            return
    raise AssertionError(
        "No Call names found in _kernel_syscall_dispatch.py via any candidate "
        "variable. Inspect the file and update the candidate list or extractor."
    )
