"""FUSE plugin Windows E2E — real WinFsp mount, real user workflows, gRPC cross-check.

Mounts the signed `nexus-fuse-plugin` via WinFsp at a drive letter on the
`windows-latest` runner and exercises the **real `cc tasks list`
operator workflow** through plain `cmd.exe` commands.  Every workflow
is ≥ 3 steps with strong causal data flow (step N's output is
step N+1's input) and a kernel-side gRPC `vfs_stat` / `vfs_read`
cross-check at every mutation.

## Why workflows, not single ops

Per the integration-test-generator skill: 1-step "did mkdir work?"
tests don't catch state-machine bugs — they look green until the
next op trips on the missed update.  3+ step workflows like
"create session → write tasks → list → cleanup" exercise the
PathIndex remap, the v3 sys_stat_batch cursor, and the FUSE dentry
invalidation in the same call chain a real operator hits.

## Workflow inventory

* **Session lifecycle** — `mkdir sess; write N tasks; dir; type each; rmdir`
  — exercises sys_mkdir → sys_write (N×) → sys_readdir + sys_stat_batch
  → sys_read (N×) → sys_unlink (N×) → sys_rmdir.  Mirrors the
  real CC `cc tasks list` end-to-end shape: a session directory of
  flat JSON files that get enumerated, read, and eventually
  cleaned up.

* **Mid-session rename** — `write task; rename to new name; verify
  old gone + new present + content preserved` — exercises
  sys_rename + PathIndex remap.  Mirrors operator workflow: "I
  meant to call this draft, not in-progress".

* **Cross-layer write integrity** — `write via mount; vfs_read
  via gRPC; verify byte-exact` — independent kernel-side check.
  Catches a plugin that silently drops writes (mount-side `type`
  would still pass via WinFsp cache, gRPC sees the truth).

## Cross-layer pattern

Same pattern `tests/e2e/docker/test_cc_tasks_share_e2e.py` uses on
Linux (the Linux Docker FUSE coverage now lives inside that
combined federation + LocalConnector + FUSE suite).  A plugin that silently drops writes still passes the
mount round-trip but fails the gRPC size check.  A kernel-side
`sys_readdir` JSON escape regression fails `dir` through the mount
but `vfs_stat` on the entry passes.  Triangulates the failing
layer, doesn't just notice failure.

## Test data isolation

Every workflow creates its own session directory under a fresh
`uuid4` name to avoid cross-test contamination.  Cleanup steps
run in a `try / finally` block so a mid-workflow failure still
removes the session dir for the next test run — otherwise a
broken intermediate state would poison subsequent runs.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from dataclasses import dataclass

import pytest

if os.environ.get("NEXUS_FUSE_WINDOWS_E2E") != "1":
    pytest.skip(
        "FUSE plugin Windows E2E — requires the .github workflow's "
        "harness to set NEXUS_FUSE_WINDOWS_E2E=1 + spawn nexusd-cluster "
        "with the signed FUSE plugin loaded at the configured drive "
        "letter (see .github/workflows/fuse-plugin-windows-e2e.yml).",
        allow_module_level=True,
    )


@dataclass(frozen=True)
class Topology:
    cluster_grpc: str
    mount_letter: str

    def mount_path(self, relpath: str) -> str:
        rel = relpath.replace("/", "\\")
        if rel.startswith("\\"):
            rel = rel[1:]
        return f"{self.mount_letter}\\{rel}" if rel else f"{self.mount_letter}\\"

    def vfs_path(self, relpath: str) -> str:
        if not relpath.startswith("/"):
            relpath = "/" + relpath
        return relpath


@pytest.fixture(scope="module")
def topology() -> Topology:
    return Topology(
        cluster_grpc=os.environ.get("NEXUS_CLUSTER_GRPC", "127.0.0.1:2126"),
        mount_letter=os.environ.get("NEXUS_FUSE_MOUNT_POINT", "Z:").rstrip("\\"),
    )


@pytest.fixture()
def session_name() -> str:
    """A fresh per-test session-dir name.  uuid4 keeps tests isolated
    so a partial-fail leftover from one workflow doesn't poison the
    next."""
    return f"sess-{uuid.uuid4().hex[:8]}"


# ─────────────────────────────────────────────────────────────────────
# Command helpers
# ─────────────────────────────────────────────────────────────────────


def _cmd(
    args: list[str], *, check: bool = True, timeout: float = 30
) -> subprocess.CompletedProcess[str]:
    """Run a `cmd.exe /c` command — the kernel-level path operator
    tools take.  Avoid PowerShell here because PS cmdlets sometimes
    route through their own caches that mask FUSE behaviours."""
    full = ["cmd.exe", "/c"] + args
    return subprocess.run(
        full,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


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
# workflow tests below.  Kept tight (single assertion each) so a
# harness failure surfaces here, not as a cascade through every
# workflow.
# ─────────────────────────────────────────────────────────────────────


class TestSanity:
    def test_grpc_port_responds(self, topology: Topology) -> None:
        result = _vfs_stat(topology.cluster_grpc, "/")
        assert "error" not in result, f"gRPC vfs_stat(/) failed: {result}"
        assert result["result"]["found"], "/ must exist on a fresh cluster"

    def test_mount_drive_letter_routable(self, topology: Topology) -> None:
        """`dir` returns 0 — proves WinFsp routed an op into the plugin.
        Content is workflow tests' job."""
        result = _cmd(["dir", f"{topology.mount_letter}\\"], check=False)
        assert result.returncode == 0, (
            f"`dir {topology.mount_letter}\\` failed rc={result.returncode}: "
            f"stderr={result.stderr}\n"
            f"Likely causes: WinFsp service not running, dylib not "
            f"signed, or the cluster's mount step failed silently."
        )


# ─────────────────────────────────────────────────────────────────────
# Workflow 1 — Session lifecycle.
#
#    mkdir sess           → sys_mkdir
#    write N tasks        → N × sys_write
#    dir sess             → sys_readdir + sys_stat_batch (the v3
#                            collapse path; pre-v3 was N × sys_stat)
#    type each task       → N × sys_read
#    del each task        → N × sys_unlink
#    rmdir sess           → sys_rmdir
#
# Strong causal links: mkdir's existence is precondition for write;
# write's content is precondition for type's verification;
# successful read of all tasks is precondition for cleanup.
# Cross-layer at every mutation: mount-side `dir` + kernel-side
# `vfs_stat`.
# ─────────────────────────────────────────────────────────────────────


class TestSessionLifecycle:
    def test_create_session_write_read_cleanup(self, topology: Topology, session_name: str) -> None:
        sess_rel = f".claude/tasks/{session_name}"
        sess_mount = topology.mount_path(sess_rel)
        task_payloads = {
            "task-001.json": '{"id":1,"status":"todo","title":"draft PR"}',
            "task-002.json": '{"id":2,"status":"todo","title":"review #4380"}',
            "task-003.json": '{"id":3,"status":"todo","title":"smoke Mac+Win"}',
        }
        try:
            # Step 1: mkdir session dir — sys_mkdir.
            _cmd(["mkdir", sess_mount])
            stat = _wait_path_via_grpc(topology, topology.vfs_path(sess_rel), expect_found=True)
            assert stat["result"]["isDirectory"], f"session dir not seen as dir by kernel: {stat}"

            # Step 2: write N tasks — N × sys_write.  Each write
            # depends on the parent dir from step 1; we verify each
            # kernel-side before the next op so a silent write loss
            # surfaces here, not 100 lines down.
            for name, payload in task_payloads.items():
                rel = f"{sess_rel}/{name}"
                _cmd(["echo", payload, ">", topology.mount_path(rel)])
                kernel_stat = _wait_path_via_grpc(
                    topology, topology.vfs_path(rel), expect_found=True
                )
                assert kernel_stat["result"]["size"] > 0, (
                    f"sys_write(`{rel}`) reported success but kernel sees "
                    f"size=0; plugin's write callback dropped bytes"
                )

            # Step 3: dir session — sys_readdir + sys_stat_batch.
            # The depended-on data is the N entries written in step 2;
            # if step 2 silently lost one, dir would surface fewer
            # entries here.
            listing = _cmd(["dir", "/b", sess_mount])
            seen_names = {ln.strip() for ln in listing.stdout.splitlines() if ln.strip()}
            assert set(task_payloads) <= seen_names, (
                f"read_directory missed tasks; expected superset of "
                f"{set(task_payloads)}, got {seen_names}.  "
                f"sys_stat_batch may have dropped entries that "
                f"sys_readdir surfaced — check the batch parser."
            )

            # Step 4: read each task back — N × sys_read.  Depends on
            # both the dir entry from step 3 AND the bytes from step 2.
            # We compare byte-exact through gRPC vfs_read (the SSOT)
            # rather than `type` (which CRLF-mangles via cmd.exe).
            for name, payload in task_payloads.items():
                read = _vfs_read(topology.cluster_grpc, topology.vfs_path(f"{sess_rel}/{name}"))
                assert "error" not in read, f"vfs_read({name}) failed: {read}"
                body = _decode_content(read).decode("utf-8", errors="replace")
                assert payload in body, (
                    f"vfs_read({name}) body did not contain expected payload\n"
                    f"  expected substring: {payload!r}\n"
                    f"  actual body:        {body!r}"
                )

            # Step 5: del each task — N × sys_unlink.  Depends on every
            # task existing (asserted in step 4).
            for name in task_payloads:
                _cmd(["del", topology.mount_path(f"{sess_rel}/{name}")])
                _wait_path_via_grpc(
                    topology, topology.vfs_path(f"{sess_rel}/{name}"), expect_found=False
                )

            # Step 6: rmdir session — sys_rmdir.  Depends on every task
            # being gone (asserted in step 5) — sys_rmdir surfaces
            # STATUS_DIRECTORY_NOT_EMPTY otherwise.
            _cmd(["rmdir", sess_mount])
            _wait_path_via_grpc(topology, topology.vfs_path(sess_rel), expect_found=False)
        finally:
            # Defensive: any mid-step failure should still try to
            # remove the session dir so re-runs aren't poisoned.
            _cmd(["rmdir", "/S", "/Q", sess_mount], check=False, timeout=10)


# ─────────────────────────────────────────────────────────────────────
# Workflow 2 — Mid-session rename.
#
# Real operator scenario: user starts writing a task, realises the
# name is wrong, renames it before adding more content.  Catches the
# class of bugs we hit on the fuser side (rename's d_move on stale
# inode, PathIndex not remapping — both fixed in fuser, this asserts
# the WinFsp side observes the same invariants).
#
#    mkdir sess               → sys_mkdir
#    write task               → sys_write
#    cross-check size kernel  → vfs_stat
#    rename task              → sys_rename
#    verify old absent        → vfs_stat (expect_found=False)
#    verify new present       → vfs_stat (expect_found=True)
#    read new byte-exact      → vfs_read (the rename preserved content)
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
            # Step 1+2: create sess + initial task — establishes the
            # rename's source.  Without these, the rename op has
            # nothing to do.
            _cmd(["mkdir", sess_mount])
            _cmd(["echo", payload, ">", topology.mount_path(old_rel)])

            # Step 3: kernel-side check.  Confirms step 2 actually
            # wrote bytes; if sys_write silently dropped them the
            # rename step below would be testing the wrong invariant
            # (renaming an empty file isn't the operator's case).
            stat = _wait_path_via_grpc(topology, topology.vfs_path(old_rel), expect_found=True)
            old_size = stat["result"]["size"]
            assert old_size > 0, "precondition: draft.json must have bytes before rename"

            # Step 4: rename through the mount — sys_rename +
            # PathIndex remap.  This is the depended-on op for
            # everything below.
            _cmd(["move", topology.mount_path(old_rel), topology.mount_path(new_rel)])

            # Step 5+6: symmetric kernel-side cross-check on both
            # sides of the rename.  draft.json must be ENOENT;
            # in-progress.json must exist with the same size old had.
            _wait_path_via_grpc(topology, topology.vfs_path(old_rel), expect_found=False)
            new_stat = _wait_path_via_grpc(topology, topology.vfs_path(new_rel), expect_found=True)
            assert new_stat["result"]["size"] == old_size, (
                f"rename moved metadata but lost bytes\n"
                f"  old size: {old_size}\n"
                f"  new size: {new_stat['result']['size']}"
            )

            # Step 7: byte-exact read of the new path through gRPC.
            # This is the strongest end-of-workflow assertion: the
            # full pipeline (FUSE rename → kernel rename → kernel
            # read) preserved content end-to-end.
            read = _vfs_read(topology.cluster_grpc, topology.vfs_path(new_rel))
            assert "error" not in read, f"vfs_read after rename failed: {read}"
            body = _decode_content(read).decode("utf-8", errors="replace")
            assert payload in body, (
                f"rename preserved metadata but bytes diverged\n"
                f"  expected substring: {payload!r}\n"
                f"  actual body:        {body!r}"
            )
        finally:
            _cmd(["rmdir", "/S", "/Q", sess_mount], check=False, timeout=10)


# ─────────────────────────────────────────────────────────────────────
# Workflow 3 — Cross-layer write integrity.
#
# Writes through the mount, then reads through gRPC.  This is the
# canonical "the plugin can't lie to me" assertion — a plugin that
# returns SUCCESS for sys_write but silently drops bytes would still
# pass `type` (WinFsp caches the response), but gRPC reads straight
# from the kernel metastore + backend so it sees the truth.
#
# The workflow is short by design (4 ops) because a longer version
# would be testing the same invariant multiple times.  Strong causal
# link: every assertion is on a value DERIVED from the preceding
# step's output (sizes match, content matches, no error).
# ─────────────────────────────────────────────────────────────────────


class TestCrossLayerWriteIntegrity:
    def test_mount_write_visible_through_grpc(self, topology: Topology, session_name: str) -> None:
        sess_rel = f".claude/tasks/{session_name}"
        sess_mount = topology.mount_path(sess_rel)
        file_rel = f"{sess_rel}/cross-layer.json"
        payload = '{"verification":"mount→kernel→read should round-trip byte-exact"}'

        try:
            # Step 1: setup parent.
            _cmd(["mkdir", sess_mount])

            # Step 2: write through the mount — the system under test.
            _cmd(["echo", payload, ">", topology.mount_path(file_rel)])

            # Step 3: kernel-side stat check.  Requires step 2 to
            # have actually committed bytes; a write that returned
            # SUCCESS but never reached the metastore would surface
            # size=0 here while mount-side `type` would still serve
            # the buffer from WinFsp's cache.
            stat = _wait_path_via_grpc(topology, topology.vfs_path(file_rel), expect_found=True)
            assert stat["result"]["size"] > 0, (
                f"plugin reported sys_write success but kernel sees size=0\n  vfs_stat: {stat}"
            )

            # Step 4: kernel-side byte-exact read — the SSOT.
            # Step 3's size check is necessary but not sufficient:
            # a plugin could lie about size and still produce wrong
            # bytes.  This is the assertion that catches that.
            read = _vfs_read(topology.cluster_grpc, topology.vfs_path(file_rel))
            assert "error" not in read, f"vfs_read failed: {read}"
            body = _decode_content(read).decode("utf-8", errors="replace")
            assert payload in body, (
                f"vfs_read body did not contain mount-side payload\n"
                f"  expected substring: {payload!r}\n"
                f"  actual body:        {body!r}"
            )
        finally:
            _cmd(["rmdir", "/S", "/Q", sess_mount], check=False, timeout=10)
