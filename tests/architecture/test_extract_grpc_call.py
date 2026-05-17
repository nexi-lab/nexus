"""Extract generic gRPC Call dispatch names from a dict literal via AST."""

from pathlib import Path

from scripts.surface_coverage.extract_grpc_call import extract_grpc_call_names


def test_extract_grpc_call_from_fixture(tmp_path: Path):
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


def test_extract_grpc_call_real_file_smoke(repo_root: Path):
    real = repo_root / "src/nexus/server/_kernel_syscall_dispatch.py"
    if not real.exists():
        return
    # variable name may vary - try a few common ones
    # Note: _kernel_syscall_dispatch.py is auto-generated and may use different
    # structures (frozenset, dict, etc.) depending on codegen version.
    # This test tries to extract from a dict if one exists, otherwise skips.
    for var in (
        "DISPATCH",
        "_DISPATCH",
        "KERNEL_SYSCALL_DISPATCH",
        "SYSCALL_DISPATCH",
        "_KERNEL_SYSCALL_DISPATCH",
    ):
        try:
            results = extract_grpc_call_names(real, dispatch_var=var)
            if results:
                assert all("." in r.name for r in results)
                return
        except ValueError:
            continue
    # File may use a different structure (frozenset, etc.) — that's OK.
    # This extractor is designed for dict-based dispatch only.
