"""Issue #4127 sandbox filesystem surface coverage gate."""

from __future__ import annotations

from scripts.surface_coverage.paths import COVERAGE_YAML
from scripts.surface_coverage.schema import ProfileStatus, load_yaml

_SUPPORTED_SANDBOX_ROWS = {
    "delete.batch",
    "filesystem.cat",
    "filesystem.delete",
    "filesystem.exists",
    "filesystem.list",
    "filesystem.ls",
    "filesystem.mkdir",
    "filesystem.read",
    "filesystem.rename",
    "filesystem.rm",
    "filesystem.rmdir",
    "filesystem.stat",
    "filesystem.write",
    "filesystem.write-batch",
    "filesystem.write_batch",
    "metadata.batch",
    "nexus_fs.sys_mkdir",
    "nexus_fs.sys_read",
    "nexus_fs.sys_readdir",
    "nexus_fs.sys_rename",
    "nexus_fs.sys_rmdir",
    "nexus_fs.sys_stat",
    "nexus_fs.sys_unlink",
    "nexus_fs.sys_write",
    "read.batch",
    "read.bulk",
    "rename.batch",
}

_SANDBOX_UNAVAILABLE_ROWS = {
    "async_files.batch_read",
    "async_files.batch_write",
    "async_files.copy",
    "async_files.copy_bulk",
    "async_files.delete",
    "async_files.exists",
    "async_files.glob",
    "async_files.grep",
    "async_files.list",
    "async_files.md_structure",
    "async_files.metadata",
    "async_files.mkdir",
    "async_files.read",
    "async_files.rename",
    "async_files.rename_batch",
    "async_files.stream",
    "async_files.write",
    "nexus_v_f_s_service.batch_read",
    "nexus_v_f_s_service.call",
    "nexus_v_f_s_service.delete",
    "nexus_v_f_s_service.ping",
    "nexus_v_f_s_service.read",
    "nexus_v_f_s_service.write",
}


def _operations_by_id():
    return {op.id: op for op in load_yaml(COVERAGE_YAML).operations}


def test_issue_4127_supported_sandbox_rows_have_docs_tests_and_perf_class():
    ops = _operations_by_id()

    missing = sorted(_SUPPORTED_SANDBOX_ROWS - set(ops))
    assert not missing

    for op_id in sorted(_SUPPORTED_SANDBOX_ROWS):
        op = ops[op_id]
        assert op.profiles["sandbox"] == ProfileStatus.SUPPORTED, op_id
        assert op.usage_example, op_id
        assert op.correctness_test, op_id
        assert op.perf_class is not None, op_id
        assert op.perf_link, op_id


def test_issue_4127_sandbox_unavailable_rows_are_classified_not_blank():
    ops = _operations_by_id()

    missing = sorted(_SANDBOX_UNAVAILABLE_ROWS - set(ops))
    assert not missing

    for op_id in sorted(_SANDBOX_UNAVAILABLE_ROWS):
        op = ops[op_id]
        assert op.profiles["sandbox"] == ProfileStatus.UNAVAILABLE, op_id
        assert op.usage_example and "sandbox" in op.usage_example.lower(), op_id
        assert op.correctness_test, op_id
        assert op.perf_class is not None, op_id
        assert op.perf_link, op_id
