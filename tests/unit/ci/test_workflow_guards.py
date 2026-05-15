from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _step(text: str, start: str, end: str | None = None) -> str:
    start_idx = text.index(start)
    end_idx = text.index(end, start_idx) if end is not None else len(text)
    return text[start_idx:end_idx]


def test_cluster_binary_uses_single_rust_cache_layer() -> None:
    workflow = (ROOT / ".github/workflows/cluster-binary-build.yml").read_text()

    toolchain_step = _step(
        workflow,
        "      - name: Install Rust toolchain\n",
        "      - name: Rust cache\n",
    )

    assert "uses: actions-rust-lang/setup-rust-toolchain@v1" in toolchain_step
    assert "cache: false" in toolchain_step


def test_docker_edge_smoke_skips_grpc_dependent_steps_without_vfs_grpc() -> None:
    workflow = (ROOT / ".github/workflows/docker-publish.yml").read_text()

    grpc_check = _step(
        workflow,
        "      - name: Check VFS gRPC availability\n",
        "      - name: Run permissions demo\n",
    )
    permissions_step = _step(
        workflow,
        "      - name: Run permissions demo\n",
        "      - name: Seed search index\n",
    )
    build_perf_step = _step(
        workflow,
        "      - name: Run build perf e2e\n",
        "      - name: Collect container logs on failure\n",
    )

    assert "id: vfs_grpc" in grpc_check
    assert 'socket.create_connection(("127.0.0.1", 2028), timeout=2)' in grpc_check
    assert "available=false" in grpc_check
    assert "if: steps.vfs_grpc.outputs.available == 'true'" in permissions_step
    assert "if: steps.vfs_grpc.outputs.available == 'true'" in build_perf_step
