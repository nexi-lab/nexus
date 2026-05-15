"""Regression tests for NexusFS.delete_batch() — Issue #4002.

Covers:
- Result keys preserve the caller's literal request path (no leading-slash
  rewrite), matching write_batch / exists_batch / read_bulk shape.
- Files written via write_batch are actually deleted (no divergence between
  the existence check inside delete_batch and the rest of the API).
- Missing files report "File not found" under the literal request key.
- Mixed present/missing batches return one entry per input path.
"""

import pytest

from nexus.contracts.metadata import DT_MOUNT
from nexus.core.nexus_fs_metadata import MetadataMixin
from tests.conftest import make_test_nexus


class _KernelShim:
    """Test shim allowing monkeypatched kernel methods.

    PyKernel is a Rust-built class with read-only attributes, so
    ``monkeypatch.setattr(nx._kernel, "metastore_X", fn)`` fails.
    Wrap the real kernel in this Python shim and monkeypatch through
    the shim instead — ``__getattr__`` falls through to the real
    kernel for unmocked methods.
    """

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)


def _wrap_kernel(nx, monkeypatch):
    """Replace ``nx._kernel`` with a Python shim so per-test
    ``monkeypatch.setattr`` calls against ``nx._kernel`` work."""
    if not isinstance(nx._kernel, _KernelShim):
        monkeypatch.setattr(nx, "_kernel", _KernelShim(nx._kernel))
    return nx._kernel


@pytest.fixture()
def nx(tmp_path):
    return make_test_nexus(tmp_path)


class TestDeleteBatchRoundTrip:
    def test_write_then_delete_then_exists(self, nx):
        path = "/delete-test-fresh.json"
        nx.write_batch([(path, b"hello")])
        assert nx.exists_batch([path]) == {path: True}

        result = nx.delete_batch([path])

        assert result == {path: {"success": True}}
        assert nx.exists_batch([path]) == {path: False}

    def test_response_key_matches_request_key_without_leading_slash(self, nx):
        path = "no-slash.json"
        nx.write_batch([(path, b"x")])

        result = nx.delete_batch([path])

        assert list(result.keys()) == [path]
        assert "/" + path not in result

    def test_response_key_matches_request_key_with_leading_slash(self, nx):
        path = "/with-slash.json"
        nx.write_batch([(path, b"x")])

        result = nx.delete_batch([path])

        assert list(result.keys()) == [path]


class TestDeleteBatchMissing:
    def test_missing_file_reports_not_found_under_request_key(self, nx):
        path = "never-existed.json"

        result = nx.delete_batch([path])

        assert result == {path: {"success": False, "error": "File not found"}}

    def test_mixed_present_and_missing(self, nx):
        present = "exists.json"
        missing = "ghost.json"
        nx.write_batch([(present, b"data")])

        result = nx.delete_batch([present, missing])

        assert result[present] == {"success": True}
        assert result[missing] == {"success": False, "error": "File not found"}
        assert set(result.keys()) == {present, missing}


class TestDeleteBatchEmpty:
    def test_empty_input_returns_empty_dict(self, nx):
        assert nx.delete_batch([]) == {}


class TestDeleteBatchImplicitDirectory:
    """Codex review: implicit directories (paths with children but no
    explicit inode) must be deletable through delete_batch when recursive."""

    def test_implicit_dir_recursive_delete(self, nx):
        nx.write_batch(
            [
                ("/parent/child1.txt", b"a"),
                ("/parent/child2.txt", b"b"),
            ]
        )
        assert nx.exists_batch(["/parent"]) == {"/parent": True}

        result = nx.delete_batch(["/parent"], recursive=True)

        assert result == {"/parent": {"success": True}}
        assert nx.exists_batch(["/parent/child1.txt", "/parent/child2.txt"]) == {
            "/parent/child1.txt": False,
            "/parent/child2.txt": False,
        }

    def test_implicit_dir_non_recursive_reports_not_empty(self, nx):
        nx.write_batch([("/parent/child.txt", b"a")])

        result = nx.delete_batch(["/parent"])

        assert result["/parent"]["success"] is False
        assert "not empty" in result["/parent"]["error"].lower()
        assert nx.exists_batch(["/parent/child.txt"]) == {"/parent/child.txt": True}


class _NonHitResult:
    hit = False
    entry_type = 99  # not 0 (not-found) and not 5 (external storage)
    post_hook_needed = False


class _NonHitKernelWrapper:
    """Wraps the real kernel but forces sys_unlink to return a non-hit
    with a nonzero entry_type — simulates write-lock timeout / internal
    abort that Codex flagged as a silent-success path."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def sys_unlink(self, *_args, **_kwargs):
        return _NonHitResult()


class TestSysUnlinkNonHit:
    """Codex review: sys_unlink must surface non-hit kernel results so
    delete_batch records a real failure instead of phantom success."""

    def test_unknown_entry_type_raises(self, nx, monkeypatch):
        from nexus.contracts.exceptions import BackendError

        nx.write_batch([("/lock-timeout.txt", b"x")])
        monkeypatch.setattr(nx, "_kernel", _NonHitKernelWrapper(nx._kernel))

        with pytest.raises(BackendError, match="entry_type=99"):
            nx.sys_unlink("/lock-timeout.txt")

    def test_delete_batch_reports_failure_on_non_hit(self, nx, monkeypatch):
        nx.write_batch([("/lock-timeout.txt", b"x")])
        monkeypatch.setattr(nx, "_kernel", _NonHitKernelWrapper(nx._kernel))

        result = nx.delete_batch(["/lock-timeout.txt"])

        assert result["/lock-timeout.txt"]["success"] is False
        assert "entry_type=99" in result["/lock-timeout.txt"]["error"]


class TestSysUnlinkMountedBackendRegistry:
    class _Kernel:
        def __init__(self, result):
            self.result = result

        def sys_stat(self, _path, _zone_id=None):
            return None

        def sys_unlink(self, *_a, **_kw):
            return self.result

        def dispatch_pre_hooks(self, *_a, **_kw):
            return None

        def has_mount(self, *_a, **_kw):
            return False

    class _Coordinator:
        def unmount(self, _path, _zone_id=None):
            return True

    class _Harness(MetadataMixin):
        _zone_id = "root"
        _hook_specs: dict[str, object] = {}

        def __init__(self, kernel):
            self._kernel = kernel
            self._driver_coordinator = TestSysUnlinkMountedBackendRegistry._Coordinator()
            self._mounted_backend_instances: dict[str, object] = {}

        def resolve_delete(self, path, *, context=None):  # noqa: ARG002
            return False, None

        def _get_context_identity(self, context):  # noqa: ARG002
            return "root", "agent", False

        def _build_rust_ctx(self, context, is_admin):  # noqa: ARG002
            return None

        def _resolve_cred(self, context):
            return context

    def test_mount_unlink_forgets_dispatch_backend(self):
        class _MountResult:
            hit = True
            entry_type = DT_MOUNT
            post_hook_needed = False

        fs = self._Harness(self._Kernel(_MountResult()))
        backend = object()
        fs._mounted_backend_instances["/mount"] = backend

        assert fs.sys_unlink("/mount") == {}
        assert fs._mounted_backend_instances == {}

    def test_external_unlink_forgets_dispatch_backend(self):
        class _ExternalResult:
            hit = False
            entry_type = 5
            post_hook_needed = False

        fs = self._Harness(self._Kernel(_ExternalResult()))
        backend = object()
        fs._mounted_backend_instances["/external-mount"] = backend

        assert fs.sys_unlink("/external-mount") == {}
        assert fs._mounted_backend_instances == {}


class TestDeleteBatchSiblingPrefixSafety:
    """Codex round-2 finding: implicit-dir recursive delete must not leak
    into sibling paths that share a string prefix."""

    def test_implicit_dir_recursive_does_not_touch_siblings(self, nx):
        nx.write_batch(
            [
                ("/parent/inside.txt", b"a"),
                ("/parent2/sibling.txt", b"b"),
                ("/parent-old/legacy.txt", b"c"),
            ]
        )

        result = nx.delete_batch(["/parent"], recursive=True)

        assert result == {"/parent": {"success": True}}
        # Only /parent/* should be gone; siblings sharing the string prefix survive.
        assert nx.exists_batch(
            ["/parent/inside.txt", "/parent2/sibling.txt", "/parent-old/legacy.txt"]
        ) == {
            "/parent/inside.txt": False,
            "/parent2/sibling.txt": True,
            "/parent-old/legacy.txt": True,
        }


class _FailingUnmountCoordinator:
    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def unmount(self, _path, _zone_id=None):
        return False


class TestDeleteBatchExternalRetrySafety:
    """Codex round-3 finding: external-storage cleanup must be retry-safe.
    metadata.delete is committed before the connector teardown so a
    partial failure cannot leave a stuck mount entry."""

    def test_kernel_unlink_before_unmount(self, nx, monkeypatch):
        """Rust kernel deletes metadata (C19) before Python calls
        coordinator.unmount. Verify the ordering is preserved."""

        order: list[str] = []

        class _ExternalResult:
            hit = False
            entry_type = 5
            post_hook_needed = False

        class _ExternalKernel:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def sys_unlink(self, *_a, **_kw):
                order.append("kernel.sys_unlink")
                return _ExternalResult()

            def dispatch_pre_hooks(self, *_a, **_kw):
                return None

        class _TrackingCoordinator:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def unmount(self, _p, _z=None):
                order.append("unmount")
                return True

        monkeypatch.setattr(nx, "_kernel", _ExternalKernel(nx._kernel))
        monkeypatch.setattr(nx, "_driver_coordinator", _TrackingCoordinator(nx._driver_coordinator))

        nx.sys_unlink("/external-mount")

        assert order == ["kernel.sys_unlink", "unmount"]

    def test_unmount_false_after_metadata_delete_succeeds(self, nx, monkeypatch):
        """If teardown returns False after the metadata commit, the call
        still succeeds (idempotent retry path) — kernel won't see et=5
        on a subsequent call."""

        class _ExternalResult:
            hit = False
            entry_type = 5
            post_hook_needed = False

        class _ExternalKernel:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def sys_unlink(self, *_a, **_kw):
                return _ExternalResult()

            def dispatch_pre_hooks(self, *_a, **_kw):
                return None

        monkeypatch.setattr(nx, "_kernel", _ExternalKernel(nx._kernel))
        monkeypatch.setattr(
            nx, "_driver_coordinator", _FailingUnmountCoordinator(nx._driver_coordinator)
        )

        # Should NOT raise — metadata is committed, teardown is best-effort.
        result = nx.sys_unlink("/external-mount")
        assert result == {}


class TestDeleteBatchImplicitNestedExplicit:
    """Codex round-3 finding: implicit recursive delete must drain
    explicit child directories leaf-first so it doesn't fail with
    Directory not empty halfway through."""

    def test_implicit_parent_with_explicit_nested_dir(self, nx):
        # /implicit-parent has no explicit inode, but contains an
        # explicit child dir (/implicit-parent/sub) with files.
        nx.write_batch(
            [
                ("/implicit-parent/sub/leaf1.txt", b"a"),
                ("/implicit-parent/sub/leaf2.txt", b"b"),
                ("/implicit-parent/top.txt", b"c"),
            ]
        )

        result = nx.delete_batch(["/implicit-parent"], recursive=True)

        assert result == {"/implicit-parent": {"success": True}}
        assert nx.exists_batch(
            [
                "/implicit-parent/sub/leaf1.txt",
                "/implicit-parent/sub/leaf2.txt",
                "/implicit-parent/top.txt",
            ]
        ) == {
            "/implicit-parent/sub/leaf1.txt": False,
            "/implicit-parent/sub/leaf2.txt": False,
            "/implicit-parent/top.txt": False,
        }


class TestDeleteHookMetadataPropagation:
    """Codex round-4 finding: post-delete hook must receive pre-delete
    FileMetadata so SnapshotWriteHook (and other rollback hooks) can
    record original state instead of returning early on metadata=None."""

    def test_delete_hook_receives_pre_delete_metadata(self, nx, monkeypatch):
        nx.write_batch([("/tracked.txt", b"original-bytes")])
        captured: list = []

        class _CapturingKernel:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def dispatch_post_hooks(self, name, ctx):
                captured.append((name, ctx))
                return self._real.dispatch_post_hooks(name, ctx)

        monkeypatch.setattr(nx, "_kernel", _CapturingKernel(nx._kernel))

        nx.delete_batch(["/tracked.txt"])

        delete_calls = [c for c in captured if c[0] == "delete"]
        assert delete_calls, "post-delete hook was not dispatched"
        ctx = delete_calls[0][1]
        assert ctx.metadata is not None, "DeleteHookContext.metadata must be populated"
        assert ctx.metadata["path"] == "/tracked.txt"
        assert ctx.metadata["size"] == len(b"original-bytes")


class TestExternalRouteRemainsFailure:
    """Codex round-4 finding: if mount route remains after teardown the
    sys_unlink must surface the failure even when metadata is committed,
    so callers can detect a real teardown failure instead of phantom
    success."""

    def test_route_remains_raises_backend_error(self, nx, monkeypatch):
        from nexus.contracts.exceptions import BackendError

        class _ExternalResult:
            hit = False
            entry_type = 5
            post_hook_needed = False

        class _LingeringMountKernel:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def sys_unlink(self, *_a, **_kw):
                return _ExternalResult()

            def dispatch_pre_hooks(self, *_a, **_kw):
                return None

            def has_mount(self, *_a, **_kw):
                return True  # route still active after "teardown"

        monkeypatch.setattr(nx, "_kernel", _LingeringMountKernel(nx._kernel))
        monkeypatch.setattr(
            nx, "_driver_coordinator", _FailingUnmountCoordinator(nx._driver_coordinator)
        )

        with pytest.raises(BackendError, match="mount route remains"):
            nx.sys_unlink("/lingering-mount")


class TestImplicitRecursiveRaceRecheck:
    """Codex round-4 finding: implicit recursive delete must re-check
    is_implicit_directory after draining children. A concurrent writer
    that adds a descendant between list_iter and finalize must surface
    as a per-item failure, not phantom success."""

    def test_concurrent_descendant_addition_is_reported(self, nx, monkeypatch):
        nx.write_batch([("/parent/leaf.txt", b"a")])

        original_unlink = nx.sys_unlink
        race_triggered = {"done": False}

        def racing_unlink(p, **kw):
            result = original_unlink(p, **kw)
            # After we've unlinked the only original child, simulate a
            # concurrent writer adding a new descendant.
            if not race_triggered["done"] and p == "/parent/leaf.txt":
                race_triggered["done"] = True
                nx.write_batch([("/parent/sneaked-in.txt", b"b")])
            return result

        monkeypatch.setattr(nx, "sys_unlink", racing_unlink)

        result = nx.delete_batch(["/parent"], recursive=True)

        assert result["/parent"]["success"] is False
        assert "concurrent" in result["/parent"]["error"].lower()

    def test_concurrent_explicit_inode_recreation_is_reported(self, nx, monkeypatch):
        """Round-5 broadened race: a writer can also create an explicit
        inode at the target itself between drain and the finalize
        re-check. Simulate the race by stubbing sys_stat to return a
        fake explicit entry AFTER the drain runs, so we exercise the
        re-check logic without needing real-time concurrency."""
        nx.write_batch([("/parent/leaf.txt", b"a")])

        original_stat = nx._kernel.sys_stat
        children_drained = {"done": False}

        def post_drain_stat(p, zone_id=None):
            if children_drained["done"] and p == "/parent":
                return {"path": "/parent", "is_directory": False, "size": 0}
            return original_stat(p, zone_id)

        original_unlink = nx.sys_unlink

        def tracking_unlink(p, **kw):
            result = original_unlink(p, **kw)
            if p == "/parent/leaf.txt":
                children_drained["done"] = True
            return result

        _wrap_kernel(nx, monkeypatch)

        monkeypatch.setattr(nx._kernel, "sys_stat", post_drain_stat)
        monkeypatch.setattr(nx, "sys_unlink", tracking_unlink)

        result = nx.delete_batch(["/parent"], recursive=True)

        assert result["/parent"]["success"] is False
        assert "concurrent" in result["/parent"]["error"].lower()


class TestPreDeleteMetadataFailClosed:
    """Codex round-9 finding: pre-delete metadata.get() must fail-closed.
    A metastore error during the probe means rollback hooks would never
    record original_hash — so refuse the delete instead of silently
    proceeding with metadata=None."""

    def test_metastore_failure_aborts_delete(self, nx, monkeypatch):
        nx.write_batch([("/tracked.txt", b"x")])

        def flaky_stat(p, _zone_id=None):
            if p == "/tracked.txt":
                raise RuntimeError("metastore degraded")
            return None

        _wrap_kernel(nx, monkeypatch)

        monkeypatch.setattr(nx._kernel, "sys_stat", flaky_stat)

        result = nx.delete_batch(["/tracked.txt"])

        assert result["/tracked.txt"]["success"] is False
        assert "metastore degraded" in result["/tracked.txt"]["error"]


class TestMountVerifyFailClosed:
    """Codex round-9 finding: has_mount errors after metadata.delete
    must fail-closed (raise) rather than silently masking a possibly
    live route as 'gone'."""

    def test_has_mount_failure_raises(self, nx, monkeypatch):
        from nexus.contracts.exceptions import BackendError

        class _ExternalResult:
            hit = False
            entry_type = 5
            post_hook_needed = False

        class _UnverifiableKernel:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def sys_unlink(self, *_a, **_kw):
                return _ExternalResult()

            def dispatch_pre_hooks(self, *_a, **_kw):
                return None

            def has_mount(self, *_a, **_kw):
                raise RuntimeError("kernel verification crashed")

        stat_dict = {
            "path": "/unverifiable",
            "content_id": None,
            "mime_type": "inode/external_storage",
            "size": 0,
            "zone_id": "root",
        }
        _wrap_kernel(nx, monkeypatch)
        monkeypatch.setattr(nx._kernel, "sys_stat", lambda _p, _z=None: stat_dict)
        monkeypatch.setattr(nx, "_kernel", _UnverifiableKernel(nx._kernel))

        with pytest.raises(BackendError, match="cannot verify mount route teardown"):
            nx.sys_unlink("/unverifiable")


class TestStrandedMountVerifyFailClosed:
    """Codex round-10 finding: et=0 stranded-mount recovery must
    fail-closed when has_mount can't be evaluated. Probe AND
    post-teardown verification both raise BackendError instead of
    silently treating verification failure as 'route absent'."""

    def test_stranded_probe_failure_raises(self, nx, monkeypatch):
        """has_mount raises during the initial probe — must surface
        BackendError, not fall through to NexusFileNotFoundError."""
        from nexus.contracts.exceptions import BackendError

        class _FlakyProbeKernel:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def sys_unlink(self, *_a, **_kw):
                class R:
                    hit = False
                    entry_type = 0
                    post_hook_needed = False

                return R()

            def has_mount(self, *_a, **_kw):
                raise RuntimeError("kernel routing degraded")

        monkeypatch.setattr(nx, "_kernel", _FlakyProbeKernel(nx._kernel))

        with pytest.raises(BackendError, match="cannot verify mount route"):
            nx.sys_unlink("/probe-fails")

    def test_stranded_post_unmount_verify_failure_raises(self, nx, monkeypatch):
        """has_mount succeeds for the initial probe (route is live)
        but the post-teardown re-check raises — must surface
        BackendError, not silently report success."""
        from nexus.contracts.exceptions import BackendError

        class _PostUnmountFlakyKernel:
            def __init__(self, real):
                self._real = real
                self._calls = 0

            def __getattr__(self, name):
                return getattr(self._real, name)

            def sys_unlink(self, *_a, **_kw):
                class R:
                    hit = False
                    entry_type = 0
                    post_hook_needed = False

                return R()

            def has_mount(self, *_a, **_kw):
                self._calls += 1
                if self._calls == 1:
                    return True  # route is live → recovery triggers
                raise RuntimeError("kernel verification crashed")

        class _RecoveringCoordinator:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def unmount(self, _p, _z=None):
                return True

        monkeypatch.setattr(nx, "_kernel", _PostUnmountFlakyKernel(nx._kernel))
        monkeypatch.setattr(
            nx, "_driver_coordinator", _RecoveringCoordinator(nx._driver_coordinator)
        )

        with pytest.raises(BackendError, match="cannot verify stranded mount route"):
            nx.sys_unlink("/post-unmount-fails")


class TestImplicitRecursiveStrictRecheck:
    """Codex round-6 finding: post-drain recheck must distinguish
    'verified absent' from 'cannot verify'. A degraded metastore that
    raises during the probe must NOT be reported as success."""

    def test_recheck_metastore_failure_reported_as_failure(self, nx, monkeypatch):
        nx.write_batch([("/parent/leaf.txt", b"a")])

        original_stat = nx._kernel.sys_stat
        children_drained = {"done": False}

        def flaky_stat(p, zone_id=None):
            if children_drained["done"] and p == "/parent":
                raise RuntimeError("metastore degraded")
            return original_stat(p, zone_id)

        original_unlink = nx.sys_unlink

        def tracking_unlink(p, **kw):
            result = original_unlink(p, **kw)
            if p == "/parent/leaf.txt":
                children_drained["done"] = True
            return result

        _wrap_kernel(nx, monkeypatch)

        monkeypatch.setattr(nx._kernel, "sys_stat", flaky_stat)
        monkeypatch.setattr(nx, "sys_unlink", tracking_unlink)

        result = nx.delete_batch(["/parent"], recursive=True)

        assert result["/parent"]["success"] is False
        assert "could not verify" in result["/parent"]["error"].lower()
        assert "metastore degraded" in result["/parent"]["error"]


class TestStrandedMountRecovery:
    """Codex round-5 finding: et=0 (kernel reports not-found) plus a
    live mount route is the signature of an earlier partial teardown.
    sys_unlink should clean up the stranded route instead of raising
    NexusFileNotFoundError and leaving an invisible orphan."""

    def test_stranded_mount_is_cleaned_up(self, nx, monkeypatch):
        unmount_calls: list[str] = []

        class _StrandedMountKernel:
            def __init__(self, real):
                self._real = real
                self._has_mount_calls = 0

            def __getattr__(self, name):
                return getattr(self._real, name)

            def sys_unlink(self, *_a, **_kw):
                class R:
                    hit = False
                    entry_type = 0  # kernel sees no entry
                    post_hook_needed = False

                return R()

            def has_mount(self, *_a, **_kw):
                # First call (probe) returns True → recovery triggers.
                # After unmount() runs, route is gone → False.
                self._has_mount_calls += 1
                return self._has_mount_calls == 1

        class _RecoveringCoordinator:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def unmount(self, p, _z=None):
                unmount_calls.append(p)
                return True

        monkeypatch.setattr(nx, "_kernel", _StrandedMountKernel(nx._kernel))
        monkeypatch.setattr(
            nx, "_driver_coordinator", _RecoveringCoordinator(nx._driver_coordinator)
        )

        result = nx.sys_unlink("/stranded-mount")

        assert result == {}
        assert unmount_calls == ["/stranded-mount"]

    def test_unmount_uses_metadata_zone_not_caller_zone(self, nx, monkeypatch):
        """Codex round-8 finding: route zone must come from pre-delete
        sys_stat result, not the caller context. An admin operating from
        the root zone deleting a tenant mount must tear down the route
        in the tenant's zone, not in root."""
        captured: list = []

        class _ExternalResult:
            hit = False
            entry_type = 5
            post_hook_needed = False

        class _ZoneRecordingKernel:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def sys_unlink(self, *_a, **_kw):
                return _ExternalResult()

            def dispatch_pre_hooks(self, *_a, **_kw):
                return None

            def has_mount(self, p, z):
                captured.append(("has_mount", p, z))
                return False

        class _ZoneRecordingCoordinator:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def unmount(self, p, z=None):
                captured.append(("unmount", p, z))
                return True

        # Pre-delete sys_stat reports the mount lives in tenant-A.
        tenant_stat = {
            "path": "/tenant-mount",
            "content_id": None,
            "mime_type": "inode/external_storage",
            "size": 0,
            "zone_id": "tenant-A",
        }
        _wrap_kernel(nx, monkeypatch)
        monkeypatch.setattr(nx._kernel, "sys_stat", lambda _p, _z=None: tenant_stat)
        monkeypatch.setattr(nx, "_kernel", _ZoneRecordingKernel(nx._kernel))
        monkeypatch.setattr(
            nx, "_driver_coordinator", _ZoneRecordingCoordinator(nx._driver_coordinator)
        )

        nx.sys_unlink("/tenant-mount")

        unmount_calls = [c for c in captured if c[0] == "unmount"]
        has_mount_calls = [c for c in captured if c[0] == "has_mount"]
        assert unmount_calls == [("unmount", "/tenant-mount", "tenant-A")]
        assert all(c[2] == "tenant-A" for c in has_mount_calls), (
            "has_mount must probe the metadata zone, not caller zone"
        )

    def test_zone_id_propagated_to_unmount_external_storage(self, nx, monkeypatch):
        """Codex round-7 finding: external-storage teardown must pass
        the caller's zone_id so non-root tenant mounts are torn down
        in their own zone instead of no-op'ing against ROOT_ZONE_ID."""
        captured: list = []

        class _ExternalResult:
            hit = False
            entry_type = 5
            post_hook_needed = False

        class _ZoneCheckingKernel:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def sys_unlink(self, *_a, **_kw):
                return _ExternalResult()

            def dispatch_pre_hooks(self, *_a, **_kw):
                return None

            def has_mount(self, _p, _z):
                return False  # cleanly torn down

        class _ZoneRecordingCoordinator:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def unmount(self, p, z=None):
                captured.append((p, z))
                return True

        monkeypatch.setattr(nx, "_kernel", _ZoneCheckingKernel(nx._kernel))
        monkeypatch.setattr(
            nx, "_driver_coordinator", _ZoneRecordingCoordinator(nx._driver_coordinator)
        )

        nx.sys_unlink("/tenant-mount")

        assert len(captured) == 1
        assert captured[0][0] == "/tenant-mount"
        # Should pass *some* zone_id (resolved from context); never call
        # without zone or the DLC default ROOT swallows tenant mounts.
        assert captured[0][1] is not None

    def test_zone_id_propagated_to_unmount_stranded_recovery(self, nx, monkeypatch):
        """Stranded-mount recovery path also propagates zone_id."""
        captured: list = []

        class _StrandedKernel:
            def __init__(self, real):
                self._real = real
                self._has_mount_calls = 0

            def __getattr__(self, name):
                return getattr(self._real, name)

            def sys_unlink(self, *_a, **_kw):
                class R:
                    hit = False
                    entry_type = 0
                    post_hook_needed = False

                return R()

            def has_mount(self, *_a, **_kw):
                self._has_mount_calls += 1
                return self._has_mount_calls == 1

        class _ZoneRecordingCoordinator:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def unmount(self, p, z=None):
                captured.append((p, z))
                return True

        monkeypatch.setattr(nx, "_kernel", _StrandedKernel(nx._kernel))
        monkeypatch.setattr(
            nx, "_driver_coordinator", _ZoneRecordingCoordinator(nx._driver_coordinator)
        )

        nx.sys_unlink("/stranded-tenant-mount")

        assert len(captured) == 1
        assert captured[0][1] is not None  # zone_id was passed

    def test_stranded_mount_recovery_failure_raises(self, nx, monkeypatch):
        from nexus.contracts.exceptions import BackendError

        class _PersistentlyStrandedKernel:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def sys_unlink(self, *_a, **_kw):
                class R:
                    hit = False
                    entry_type = 0
                    post_hook_needed = False

                return R()

            def has_mount(self, *_a, **_kw):
                return True  # route never goes away

        class _NoOpUnmount:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def unmount(self, _p, _z=None):
                return True  # claims success but route remains

        monkeypatch.setattr(nx, "_kernel", _PersistentlyStrandedKernel(nx._kernel))
        monkeypatch.setattr(nx, "_driver_coordinator", _NoOpUnmount(nx._driver_coordinator))

        with pytest.raises(BackendError, match="stranded mount route remains"):
            nx.sys_unlink("/persistently-stranded")


class TestDeleteBatchImplicitDirProbeFailure:
    """Codex round-2 finding: a degraded is_implicit_directory probe must
    record per-path failure, not abort the rest of the batch."""

    def test_implicit_dir_probe_failure_isolated(self, nx, monkeypatch):
        nx.write_batch([("/keeper.txt", b"x")])

        original_stat = nx._kernel.sys_stat

        def flaky_stat(p, zone_id=None):
            if p == "/ghost":
                raise RuntimeError("metastore degraded")
            return original_stat(p, zone_id)

        _wrap_kernel(nx, monkeypatch)

        monkeypatch.setattr(nx._kernel, "sys_stat", flaky_stat)

        # /ghost doesn't exist → sys_unlink raises NexusFileNotFoundError →
        # the probe runs and raises. Must record per-item failure and
        # still process /keeper.txt.
        result = nx.delete_batch(["/ghost", "/keeper.txt"])

        assert result["/ghost"]["success"] is False
        assert "metastore degraded" in result["/ghost"]["error"]
        assert result["/keeper.txt"] == {"success": True}
