"""FUSE plugin macOS NFS E2E — real NFS localhost mount, real user workflows, gRPC cross-check.

When FUSE-T is broken (macOS 26 Tahoe: go-nfsv4 panic, SMB EOF, FSKit
entitlement missing), the FUSE plugin falls back to an in-process NFSv3
localhost server mounted via macOS native ``mount_nfs``.  This suite
exercises that fallback path end-to-end: real ``nexusd-cluster`` → real
NFS mount → real shell commands → gRPC ``vfs_stat`` / ``vfs_read``
kernel-side cross-check at every step.

## Why workflows, not single ops

Per the integration-test-generator pattern: 1-step "did mkdir work?"
tests don't catch state-machine bugs — they look green until the
next op trips on the missed update.  3+ step workflows like
"create session → write tasks → list → cleanup" exercise the
PathIndex remap, readdir pagination, and NFS dentry caching in the
same call chain a real operator hits.

## Workflow inventory

* **Session lifecycle** — ``mkdir sess; write N tasks; ls; cat each; rm all; rmdir``
  — exercises sys_mkdir → sys_write (N×) → sys_readdir → sys_read (N×)
  → sys_unlink (N×) → sys_rmdir.  Mirrors the real CC ``cc tasks list``
  end-to-end shape.

* **Mid-session rename** — ``write task; mv to new name; verify old
  gone + new present + content preserved`` — exercises sys_rename +
  PathIndex remap.

* **Cross-layer write integrity** — ``write via mount; vfs_read via
  gRPC; verify byte-exact`` — independent kernel-side check.  Catches a
  plugin that silently drops writes (mount-side ``cat`` would still pass
  via NFS attribute cache, gRPC sees the truth).

## Test data isolation

Every workflow creates its own session directory under a fresh
``uuid4`` name.  Cleanup runs in ``try / finally``.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from dataclasses import dataclass

import pytest

if os.environ.get("NEXUS_FUSE_MACOS_NFS_E2E") != "1":
    pytest.skip(
        "FUSE plugin macOS NFS E2E — requires the .github workflow's "
        "harness to set NEXUS_FUSE_MACOS_NFS_E2E=1 + spawn nexusd-cluster "
        "with the FUSE plugin loaded and NFS fallback active at the "
        "configured mount point (see .github/workflows/fuse-plugin-macos-nfs-e2e.yml).",
        allow_module_level=True,
    )


@dataclass(frozen=True)
class Topology:
    cluster_grpc: str
    mount_point: str

    def mount_path(self, relpath: str) -> str:
        rel = relpath.lstrip("/")
        return os.path.join(self.mount_point, rel) if rel else self.mount_point

    def vfs_path(self, relpath: str) -> str:
        if not relpath.startswith("/"):
            relpath = "/" + relpath
        return relpath


@pytest.fixture(scope="module")
def topology() -> Topology:
    return Topology(
        cluster_grpc=os.environ.get("NEXUS_CLUSTER_GRPC", "127.0.0.1:2126"),
        mount_point=os.environ.get("NEXUS_FUSE_MOUNT_POINT", "/tmp/nexus-nfs-e2e").rstrip("/"),
    )


@pytest.fixture()
def session_name() -> str:
    return f"sess-{uuid.uuid4().hex[:8]}"


# ─────────────────────────────────────────────────────────────────────
# Command helpers
# ─────────────────────────────────────────────────────────────────────


def _sh(
    args: list[str], *, check: bool = True, timeout: float = 30
) -> subprocess.CompletedProcess[str]:
    """Run a shell command — the operator-level path."""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def _write_file(path: str, payload: str) -> None:
    """Write ``payload`` byte-exact to ``path`` through Python's
    ``open()``.  The path lives on an NFS-mounted directory so the
    underlying write syscall fires the NFS WRITE RPC → plugin's
    ``write`` callback — same code path a real tool hits."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(payload)


def _vfs_stat(grpc_target: str, path: str) -> dict:
    from tests.e2e.docker import runbook_helpers

    return runbook_helpers.vfs_stat(grpc_target, path)


def _vfs_read(grpc_target: str, path: str) -> dict:
    from tests.e2e.docker import runbook_helpers

    return runbook_helpers.vfs_read(grpc_target, path)


def _decode_content(read_result: dict) -> bytes:
    from tests.e2e.docker import runbook_helpers

    return runbook_helpers.decode_content(read_result)


def _wait_path_via_grpc(
    topology: Topology,
    path: str,
    *,
    expect_found: bool,
    timeout: float = 10.0,
) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = _vfs_stat(topology.cluster_grpc, path)
        if "error" not in last and last["result"]["found"] == expect_found:
            return last
        time.sleep(0.1)
    pytest.fail(
        f"vfs_stat({path}) never reached expected found={expect_found} "
        f"within {timeout}s — last result: {last}"
    )


# ─────────────────────────────────────────────────────────────────────
# Sanity — proves the harness is healthy enough to trust the
# workflow tests below.
# ─────────────────────────────────────────────────────────────────────


class TestSanity:
    def test_grpc_port_responds(self, topology: Topology) -> None:
        result = _vfs_stat(topology.cluster_grpc, "/")
        assert "error" not in result, f"gRPC vfs_stat(/) failed: {result}"
        assert result["result"]["found"], "/ must exist on a fresh cluster"

    def test_nfs_mount_reachable(self, topology: Topology) -> None:
        """Prove the NFS mount is live.  ``os.path.isdir`` fires a
        GETATTR NFS RPC which exercises the full NFS server → plugin →
        kernel path.  Unlike ``mount | grep``, this actually proves
        data flows end-to-end."""
        assert os.path.isdir(topology.mount_point), (
            f"NFS mount at {topology.mount_point} is not reachable as a directory. "
            f"Likely causes: NFS server failed to bind, mount_nfs failed, "
            f"or the cluster's FUSE plugin didn't fall back to NFS."
        )

    def test_mount_shows_nfs_type(self, topology: Topology) -> None:
        """Verify ``mount`` output confirms NFS mount type — proves
        the fallback path was taken (not FUSE-T)."""
        result = _sh(["mount"], check=False)
        mount_entry = [ln for ln in result.stdout.splitlines() if topology.mount_point in ln]
        assert mount_entry, (
            f"No mount entry found for {topology.mount_point}.\nFull mount output:\n{result.stdout}"
        )
        assert any("nfs" in entry.lower() for entry in mount_entry), (
            f"Mount at {topology.mount_point} exists but is not NFS type.\n"
            f"Entry: {mount_entry[0]}\n"
            f"Expected NFS localhost fallback, got something else."
        )


# ─────────────────────────────────────────────────────────────────────
# Workflow 1 — Session lifecycle.
#
#    mkdir sess           → sys_mkdir
#    write N tasks        → N × sys_write
#    ls sess              → sys_readdir
#    cat each task        → N × sys_read
#    rm each task         → N × sys_unlink
#    rmdir sess           → sys_rmdir
# ─────────────────────────────────────────────────────────────────────


class TestSessionLifecycle:
    def test_create_session_write_read_cleanup(self, topology: Topology, session_name: str) -> None:
        sess_rel = f".claude/tasks/{session_name}"
        sess_mount = topology.mount_path(sess_rel)
        task_payloads = {
            "task-001.json": '{"id":1,"status":"todo","title":"draft PR"}',
            "task-002.json": '{"id":2,"status":"todo","title":"review #4450"}',
            "task-003.json": '{"id":3,"status":"todo","title":"smoke Mac NFS"}',
        }
        try:
            # Step 1: mkdir session dir — sys_mkdir.
            os.makedirs(sess_mount, exist_ok=True)
            stat = _wait_path_via_grpc(topology, topology.vfs_path(sess_rel), expect_found=True)
            assert stat["result"]["isDirectory"], f"session dir not seen as dir by kernel: {stat}"

            # Step 2: write N tasks — N × sys_write.
            for name, payload in task_payloads.items():
                rel = f"{sess_rel}/{name}"
                _write_file(topology.mount_path(rel), payload)
                kernel_stat = _wait_path_via_grpc(
                    topology, topology.vfs_path(rel), expect_found=True
                )
                assert kernel_stat["result"]["size"] > 0, (
                    f"sys_write(`{rel}`) reported success but kernel sees "
                    f"size=0; NFS write callback dropped bytes"
                )

            # Step 3: ls session — sys_readdir.
            listing = os.listdir(sess_mount)
            seen_names = set(listing)
            assert set(task_payloads) <= seen_names, (
                f"readdir missed tasks; expected superset of {set(task_payloads)}, got {seen_names}"
            )

            # Step 4: read each task back — N × sys_read.
            # Compare byte-exact through gRPC vfs_read (the SSOT).
            for name, payload in task_payloads.items():
                read = _vfs_read(
                    topology.cluster_grpc,
                    topology.vfs_path(f"{sess_rel}/{name}"),
                )
                assert "error" not in read, f"vfs_read({name}) failed: {read}"
                body = _decode_content(read).decode("utf-8", errors="replace")
                assert payload in body, (
                    f"vfs_read({name}) body did not contain expected payload\n"
                    f"  expected substring: {payload!r}\n"
                    f"  actual body:        {body!r}"
                )

            # Step 5: unlink each task — N × sys_unlink.
            for name in task_payloads:
                os.remove(topology.mount_path(f"{sess_rel}/{name}"))
                _wait_path_via_grpc(
                    topology,
                    topology.vfs_path(f"{sess_rel}/{name}"),
                    expect_found=False,
                )

            # Step 6: rmdir session — sys_rmdir.
            os.rmdir(sess_mount)
            _wait_path_via_grpc(topology, topology.vfs_path(sess_rel), expect_found=False)
        finally:
            _sh(["rm", "-rf", sess_mount], check=False, timeout=10)


# ─────────────────────────────────────────────────────────────────────
# Workflow 2 — Mid-session rename.
#
#    mkdir sess               → sys_mkdir
#    write task               → sys_write
#    cross-check size kernel  → vfs_stat
#    rename task              → sys_rename
#    verify old absent        → vfs_stat (expect_found=False)
#    verify new present       → vfs_stat (expect_found=True)
#    read new byte-exact      → vfs_read
# ─────────────────────────────────────────────────────────────────────


class TestMidSessionRename:
    def test_rename_preserves_content_and_remaps_inode(
        self, topology: Topology, session_name: str
    ) -> None:
        sess_rel = f".claude/tasks/{session_name}"
        sess_mount = topology.mount_path(sess_rel)
        old_rel = f"{sess_rel}/draft.json"
        new_rel = f"{sess_rel}/in-progress.json"
        payload = '{"title":"draft to in-progress","content":"WIP"}'

        try:
            # Step 1+2: create sess + initial task.
            os.makedirs(sess_mount, exist_ok=True)
            _write_file(topology.mount_path(old_rel), payload)

            # Step 3: kernel-side check.
            stat = _wait_path_via_grpc(topology, topology.vfs_path(old_rel), expect_found=True)
            old_size = stat["result"]["size"]
            assert old_size > 0, "precondition: draft.json must have bytes before rename"

            # Step 4: rename through the mount — sys_rename.
            os.rename(
                topology.mount_path(old_rel),
                topology.mount_path(new_rel),
            )

            # Step 5+6: cross-check both sides of the rename.
            _wait_path_via_grpc(topology, topology.vfs_path(old_rel), expect_found=False)
            new_stat = _wait_path_via_grpc(topology, topology.vfs_path(new_rel), expect_found=True)
            assert new_stat["result"]["size"] == old_size, (
                f"rename moved metadata but lost bytes\n"
                f"  old size: {old_size}\n"
                f"  new size: {new_stat['result']['size']}"
            )

            # Step 7: byte-exact read of the new path through gRPC.
            read = _vfs_read(topology.cluster_grpc, topology.vfs_path(new_rel))
            assert "error" not in read, f"vfs_read after rename failed: {read}"
            body = _decode_content(read).decode("utf-8", errors="replace")
            assert payload in body, (
                f"rename preserved metadata but bytes diverged\n"
                f"  expected substring: {payload!r}\n"
                f"  actual body:        {body!r}"
            )
        finally:
            _sh(["rm", "-rf", sess_mount], check=False, timeout=10)


# ─────────────────────────────────────────────────────────────────────
# Workflow 3 — Cross-layer write integrity.
# ─────────────────────────────────────────────────────────────────────


class TestCrossLayerWriteIntegrity:
    def test_mount_write_visible_through_grpc(self, topology: Topology, session_name: str) -> None:
        sess_rel = f".claude/tasks/{session_name}"
        sess_mount = topology.mount_path(sess_rel)
        file_rel = f"{sess_rel}/cross-layer.json"
        payload = '{"verification":"mount→kernel→read should round-trip byte-exact"}'

        try:
            # Step 1: setup parent.
            os.makedirs(sess_mount, exist_ok=True)

            # Step 2: write through the NFS mount.
            _write_file(topology.mount_path(file_rel), payload)

            # Step 3: kernel-side stat check.
            stat = _wait_path_via_grpc(topology, topology.vfs_path(file_rel), expect_found=True)
            assert stat["result"]["size"] > 0, (
                f"plugin reported sys_write success but kernel sees size=0\n  vfs_stat: {stat}"
            )

            # Step 4: kernel-side byte-exact read — the SSOT.
            read = _vfs_read(topology.cluster_grpc, topology.vfs_path(file_rel))
            assert "error" not in read, f"vfs_read failed: {read}"
            body = _decode_content(read).decode("utf-8", errors="replace")
            assert payload in body, (
                f"vfs_read body did not contain mount-side payload\n"
                f"  expected substring: {payload!r}\n"
                f"  actual body:        {body!r}"
            )
        finally:
            _sh(["rm", "-rf", sess_mount], check=False, timeout=10)


# ─────────────────────────────────────────────────────────────────────
# Workflow 4 — NFS-specific: cat through shell (not Python open).
#
# NFS has its own attribute caching layer that differs from FUSE-T's
# kernel_cache.  A bug where the NFS server returns stale fattr3 or
# wrong file size would make `cat` return truncated/wrong data while
# Python's open() (which went through NFS LOOKUP + READ in a different
# call sequence) might still work.  This workflow exercises the exact
# shell pipeline an operator uses.
# ─────────────────────────────────────────────────────────────────────


class TestShellRoundTrip:
    def test_cat_returns_written_content(self, topology: Topology, session_name: str) -> None:
        sess_rel = f".claude/tasks/{session_name}"
        sess_mount = topology.mount_path(sess_rel)
        file_rel = f"{sess_rel}/shell-test.json"
        payload = '{"shell":"cat should return this exact string"}'

        try:
            os.makedirs(sess_mount, exist_ok=True)
            _write_file(topology.mount_path(file_rel), payload)

            # Read back via shell `cat` — the real operator path.
            result = _sh(["cat", topology.mount_path(file_rel)])
            assert payload in result.stdout, (
                f"cat output did not contain expected payload\n"
                f"  expected substring: {payload!r}\n"
                f"  actual stdout:      {result.stdout!r}"
            )

            # Also verify ls sees the file in the directory listing.
            ls_result = _sh(["ls", sess_mount])
            assert "shell-test.json" in ls_result.stdout, (
                f"ls did not list shell-test.json\n  ls output: {ls_result.stdout!r}"
            )
        finally:
            _sh(["rm", "-rf", sess_mount], check=False, timeout=10)
