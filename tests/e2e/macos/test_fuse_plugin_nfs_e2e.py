"""FUSE plugin macOS mount E2E — real mount, real user workflows.

Exercises the FUSE plugin's mount path on macOS end-to-end.  When
FUSE-T works, it mounts via fuser; when FUSE-T is broken (macOS 26
Tahoe), the plugin falls back to NFS localhost.  Either way, this
suite validates that the mount is live and file ops round-trip
correctly through the plugin → KernelHandle → kernel pipeline.

## Why mount-side verification (not gRPC cross-check)

The ``dynamic`` bootstrap mode starts nexusd-cluster without a Raft
zone.  The FUSE/NFS mount writes to the kernel's PathLocalBackend
directly, but gRPC ``vfs_stat`` / ``vfs_read`` route through the
zone-aware Stat/Read RPCs which return ``found=False`` when no zone
exists.  Mount-side read-back (via ``open()`` / ``cat``) exercises
the same KernelHandle → kernel → backend pipeline as gRPC, just
without the zone routing layer.

## Workflow inventory

* **Session lifecycle** — mkdir → write N → ls → read each → rm → rmdir
* **Mid-session rename** — write → rename → verify old gone + new present
* **Shell round-trip** — write → cat → ls (shell-level verification)
"""

from __future__ import annotations

import os
import subprocess
import uuid
from dataclasses import dataclass

import pytest

if os.environ.get("NEXUS_FUSE_MACOS_NFS_E2E") != "1":
    pytest.skip(
        "FUSE plugin macOS NFS E2E — requires NEXUS_FUSE_MACOS_NFS_E2E=1",
        allow_module_level=True,
    )


@dataclass(frozen=True)
class Topology:
    mount_point: str

    def mount_path(self, relpath: str) -> str:
        rel = relpath.lstrip("/")
        return os.path.join(self.mount_point, rel) if rel else self.mount_point


@pytest.fixture(scope="module")
def topology() -> Topology:
    return Topology(
        mount_point=os.environ.get("NEXUS_FUSE_MOUNT_POINT", "/tmp/nexus-nfs-e2e").rstrip("/"),
    )


@pytest.fixture()
def session_name() -> str:
    return f"sess-{uuid.uuid4().hex[:8]}"


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _sh(
    args: list[str], *, check: bool = True, timeout: float = 30
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=check)


def _write_file(path: str, payload: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(payload)


def _read_file(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


# ─────────────────────────────────────────────────────────────────────
# Sanity
# ─────────────────────────────────────────────────────────────────────


class TestSanity:
    def test_mount_reachable(self, topology: Topology) -> None:
        """Prove the mount is live — ``os.path.isdir`` fires a real
        VFS op through the plugin."""
        assert os.path.isdir(topology.mount_point), (
            f"Mount at {topology.mount_point} is not reachable. "
            f"Plugin may not have loaded or mount failed."
        )

    def test_mount_is_listable(self, topology: Topology) -> None:
        """``ls`` fires readdir through the plugin."""
        result = _sh(["ls", topology.mount_point], check=False)
        assert result.returncode == 0, f"ls {topology.mount_point} failed: {result.stderr}"


# ─────────────────────────────────────────────────────────────────────
# Workflow 1 — Session lifecycle (6 steps, 3+ causal links).
# ─────────────────────────────────────────────────────────────────────


class TestSessionLifecycle:
    def test_create_session_write_read_cleanup(self, topology: Topology, session_name: str) -> None:
        sess_mount = topology.mount_path(f".claude/tasks/{session_name}")
        task_payloads = {
            "task-001.json": '{"id":1,"status":"todo","title":"draft PR"}',
            "task-002.json": '{"id":2,"status":"todo","title":"review #4450"}',
            "task-003.json": '{"id":3,"status":"todo","title":"smoke Mac"}',
        }
        try:
            # Step 1: mkdir — sys_mkdir.
            os.makedirs(sess_mount, exist_ok=True)
            assert os.path.isdir(sess_mount)

            # Step 2: write N tasks — sys_write.
            for name, payload in task_payloads.items():
                _write_file(os.path.join(sess_mount, name), payload)

            # Step 3: ls — sys_readdir.
            listing = set(os.listdir(sess_mount))
            assert set(task_payloads) <= listing, (
                f"readdir missed tasks: expected {set(task_payloads)}, got {listing}"
            )

            # Step 4: read each back — sys_read (byte-exact).
            for name, payload in task_payloads.items():
                body = _read_file(os.path.join(sess_mount, name))
                assert body == payload, (
                    f"read({name}) mismatch\n  expected: {payload!r}\n  actual: {body!r}"
                )

            # Step 5: unlink each — sys_unlink.
            for name in task_payloads:
                os.remove(os.path.join(sess_mount, name))
                assert not os.path.exists(os.path.join(sess_mount, name))

            # Step 6: rmdir — sys_rmdir.
            os.rmdir(sess_mount)
            assert not os.path.exists(sess_mount)
        finally:
            _sh(["rm", "-rf", sess_mount], check=False, timeout=10)


# ─────────────────────────────────────────────────────────────────────
# Workflow 2 — Mid-session rename (5 steps).
# ─────────────────────────────────────────────────────────────────────


class TestMidSessionRename:
    def test_rename_preserves_content_and_remaps_inode(
        self, topology: Topology, session_name: str
    ) -> None:
        sess_mount = topology.mount_path(f".claude/tasks/{session_name}")
        old_path = os.path.join(sess_mount, "draft.json")
        new_path = os.path.join(sess_mount, "in-progress.json")
        payload = '{"title":"draft to in-progress","content":"WIP"}'

        try:
            # Step 1+2: create sess + write initial task.
            os.makedirs(sess_mount, exist_ok=True)
            _write_file(old_path, payload)
            assert os.path.exists(old_path)

            # Step 3: rename — sys_rename.
            os.rename(old_path, new_path)

            # Step 4: old gone, new present.
            assert not os.path.exists(old_path), "old path still exists after rename"
            assert os.path.exists(new_path), "new path missing after rename"

            # Step 5: content preserved.
            body = _read_file(new_path)
            assert body == payload, (
                f"rename lost content\n  expected: {payload!r}\n  actual: {body!r}"
            )
        finally:
            _sh(["rm", "-rf", sess_mount], check=False, timeout=10)


# ─────────────────────────────────────────────────────────────────────
# Workflow 3 — Shell round-trip (cat + ls).
# ─────────────────────────────────────────────────────────────────────


class TestShellRoundTrip:
    def test_cat_returns_written_content(self, topology: Topology, session_name: str) -> None:
        sess_mount = topology.mount_path(f".claude/tasks/{session_name}")
        file_path = os.path.join(sess_mount, "shell-test.json")
        payload = '{"shell":"cat should return this exact string"}'

        try:
            os.makedirs(sess_mount, exist_ok=True)
            _write_file(file_path, payload)

            # Read back via shell `cat`.
            result = _sh(["cat", file_path])
            assert payload in result.stdout, (
                f"cat mismatch\n  expected: {payload!r}\n  actual: {result.stdout!r}"
            )

            # ls sees the file.
            ls_result = _sh(["ls", sess_mount])
            assert "shell-test.json" in ls_result.stdout
        finally:
            _sh(["rm", "-rf", sess_mount], check=False, timeout=10)
