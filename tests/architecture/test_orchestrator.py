"""Orchestrator integration test: end-to-end extraction against fixture tree."""

from pathlib import Path

from scripts.gen_api_surface_coverage import generate_coverage
from scripts.surface_coverage.schema import load_yaml


def _build_fixture_tree(root: Path) -> None:
    """Build a tiny repo mirror with one of each surface type."""
    (root / "src/nexus/cli/commands").mkdir(parents=True)
    (root / "src/nexus/cli/commands/__init__.py").write_text(
        '_REGISTER_COMMANDS = {"file_ops": ("read", "write")}\n'
    )
    (root / "src/nexus/cli/commands/file_ops.py").write_text("# fake\n")

    (root / "src/nexus/server").mkdir(parents=True)
    (root / "src/nexus/server/_kernel_syscall_dispatch.py").write_text(
        'KERNEL_SYSCALL_NAMES = frozenset({"read", "write"})\n'
    )

    # HTTP routes live under server/api/ (v3 recursive scan)
    (root / "src/nexus/server/api").mkdir(parents=True)
    (root / "src/nexus/server/api/routes.py").write_text(
        "class _R:\n"
        "    def get(self, p):\n"
        "        def deco(f): return f\n"
        "        return deco\n"
        "    def post(self, p): return self.get(p)\n"
        "router = _R()\n"
        "@router.post('/api/v1/filesystem/read')\n"
        "def read(): pass\n"
        "@router.post('/api/v1/filesystem/write')\n"
        "def write(): pass\n"
    )

    (root / "src/nexus/config").mkdir(parents=True)
    (root / "src/nexus/config/tool_profiles.yaml").write_text(
        "profiles:\n  default:\n    tools: [nexus_filesystem_read, nexus_filesystem_write]\n"
    )

    (root / "src/nexus/contracts").mkdir(parents=True)
    (root / "src/nexus/contracts/deployment_profile.py").write_text(
        "from enum import Enum\n"
        "class DeploymentProfile(str, Enum):\n"
        '    LITE="lite"\n    SANDBOX="sandbox"\n    FULL="full"\n'
    )

    # SDK: walked remote/ tree (v3) discovers FilesystemClient
    (root / "src/nexus/remote/clients").mkdir(parents=True)
    (root / "src/nexus/remote/clients/fs.py").write_text(
        "class FilesystemClient:\n"
        "    def filesystem_read(self): pass\n"
        "    def filesystem_write(self): pass\n"
    )

    (root / "proto/nexus/grpc/vfs").mkdir(parents=True)
    (root / "proto/nexus/grpc/vfs/vfs.proto").write_text(
        "service Filesystem { rpc Read (R) returns (R); rpc Write (R) returns (R); }\n"
    )


def test_orchestrator_end_to_end(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _build_fixture_tree(repo)

    out = tmp_path / "coverage.yaml"
    generate_coverage(repo_root=repo, output=out, overrides=None)
    coverage = load_yaml(out)

    op_ids = {op.id for op in coverage.operations}
    # filesystem.read should be present (CLI, HTTP, MCP, gRPC typed, SDK, grpc_call)
    assert "filesystem.read" in op_ids, f"expected filesystem.read; ops={sorted(op_ids)}"
    assert "filesystem.write" in op_ids, f"expected filesystem.write; ops={sorted(op_ids)}"

    read_op = next(op for op in coverage.operations if op.id == "filesystem.read")
    assert read_op is not None
    # Should aggregate cells from multiple transports
    assert "cli" in read_op.transports
    assert "http" in read_op.transports
    assert "mcp" in read_op.transports
    assert "grpc_typed" in read_op.transports
    assert "sdk" in read_op.transports
    # grpc_call: flat "read" should be mapped to filesystem.read via heuristic
    assert "grpc_call" in read_op.transports


def test_orchestrator_idempotent(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _build_fixture_tree(repo)
    out = tmp_path / "coverage.yaml"
    generate_coverage(repo_root=repo, output=out, overrides=None)
    first = out.read_text()
    generate_coverage(repo_root=repo, output=out, overrides=None)
    second = out.read_text()
    assert first == second
