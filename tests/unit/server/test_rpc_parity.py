#!/usr/bin/env python3
"""Test that all @rpc_expose methods are reachable via REMOTE profile.

With RemoteServiceProxy (Issue #1171), all service methods are universally
forwarded via __getattr__. The parity guarantee is now structural:
- Every @rpc_expose method is callable via the server's RPC dispatch table.
- The client's RemoteServiceProxy forwards any method name to _call_rpc().

This test verifies the server side: all public NexusFS methods are either
@rpc_expose decorated or explicitly excluded from RPC.

Issue #1065: Replaces the old RemoteNexusFS parity test — the universal
proxy makes per-method client coverage unnecessary.
"""

import inspect
from pathlib import Path

import pytest

from nexus.core.nexus_fs import NexusFS


def get_rpc_exposed_methods(cls):
    """Get all methods marked with @rpc_expose decorator.

    Returns:
        dict: Mapping of method name to method object
    """
    exposed = {}
    for name in dir(cls):
        if name.startswith("_"):
            continue
        try:
            attr = getattr(cls, name)
            if callable(attr) and hasattr(attr, "_rpc_exposed"):
                # Use the RPC name if specified, otherwise use method name
                rpc_name = getattr(attr, "_rpc_name", name)
                exposed[rpc_name] = attr
        except Exception:
            continue
    return exposed


def get_all_rpc_exposed_methods():
    """Get all @rpc_expose methods from NexusFS and brick services.

    Issue #2035, Follow-up 1: Skills RPC methods live on brick services,
    not on NexusFS. This scans all sources the server would scan.
    """
    exposed = get_rpc_exposed_methods(NexusFS)

    # Issue #1410: Version methods moved from NexusFS to VersionService
    from nexus.bricks.versioning.version_service import VersionService

    exposed.update(get_rpc_exposed_methods(VersionService))

    return exposed


def test_remote_service_proxy_coverage():
    """Verify RemoteServiceProxy can forward all @rpc_expose methods.

    With the universal proxy (Issue #1171), any method name is forwarded
    to _call_rpc(). This test verifies the server-side dispatch table
    has all the expected methods.
    """
    exposed_methods = get_all_rpc_exposed_methods()

    # Sanity check: we should have a reasonable number of exposed methods
    assert len(exposed_methods) > 30, (
        f"Expected at least 30 @rpc_expose methods, found {len(exposed_methods)}. "
        "This might indicate a scanning issue."
    )

    # Verify key method categories are present (only @rpc_expose methods)
    # Note: list/glob/grep/workspace_snapshot are ABC stubs that delegate
    # to services — they aren't @rpc_expose on NexusFS itself.
    expected_categories = {
        "File I/O": ["sys_read", "sys_write", "sys_unlink"],
        "Directory": ["mkdir", "rmdir"],
        "Query": ["access", "sys_stat"],
        "Versioning": ["get_version", "list_versions"],
    }

    for category, methods in expected_categories.items():
        for method in methods:
            assert method in exposed_methods, (
                f"Expected @rpc_expose method '{method}' ({category}) not found. "
                "RemoteServiceProxy forwards all method names, but the server "
                "must expose the method via @rpc_expose for it to be callable."
            )

    print(f"\n✓ All {len(exposed_methods)} @rpc_expose methods are forwarded by RemoteServiceProxy")


def test_all_public_methods_are_exposed_or_excluded():
    """ENFORCEMENT: All public methods MUST be @rpc_expose or explicitly excluded.

    This test ensures that developers don't forget to expose new methods via RPC.
    If a method should NOT be exposed, it must be added to INTERNAL_ONLY_METHODS.

    This prevents issues where new functionality is added locally but not exposed remotely.
    """
    # Methods that are intentionally NOT exposed via RPC
    # ADD NEW METHODS HERE if they should remain local-only
    INTERNAL_ONLY_METHODS = {
        # Lifecycle/infrastructure methods
        "aclose",  # Async shutdown — stop BackgroundService + unregister hooks (Issue #1580)
        "close",  # Connection management - handled differently for remote
        "link",  # Boot phase 1 - pure memory wiring, not an RPC operation
        "initialize",  # Boot phase 2 - one-time side effects, not an RPC operation
        "bootstrap",  # Boot phase 3 - async task startup, server-only
        "service",  # ServiceRegistry lookup — local kernel API, not an RPC operation (Issue #1452)
        "service_coordinator",  # ServiceRegistry lifecycle access — server-only (Issue #1452 Phase 3)
        "swap_service",  # Hot-swap via coordinator — server-only admin operation (Issue #1452 Phase 3)
        "load_all_saved_mounts",  # Internal initialization method - called automatically on startup
        # Tier 2 convenience wrappers — delegate to sys_lock/sys_unlock which ARE @rpc_expose
        "lock",  # Tier 2 blocking wait over sys_lock (defined in NexusFS)
        "unlock",  # Tier 2 alias for sys_unlock (defined in NexusFS)
        "locked",  # Tier 2 async context manager for lock/unlock (defined in NexusFS)
        # Server-side only methods (clients get this via HTTP headers)
        "get_etag",  # Returns ETag for early 304 check - clients receive ETags via HTTP headers on read
        # Async methods - TODO: Add async RPC support
        # Tracked in issue #XXX
        "parse",  # Async - requires async RPC support
        # Already exposed via different mechanism
        "write_batch",  # Exposed via different RPC endpoint
        # Tiger Cache internal methods - server-side optimization only
        "grant_traverse_on_implicit_dirs",  # Internal - grants TRAVERSE on implicit dirs during init
        "process_tiger_cache_queue",  # Internal - background worker processes cache updates
        "warm_tiger_cache",  # Internal - pre-computes permissions for cache warming
        # Phase 2 Service Composition - Async delegation methods (Issue #988)
        # These are internal async methods that delegate to services. The original
        # sync mixin methods (without "a" prefix) already have @rpc_expose decorators.
        # VersionService delegation (removed: sync wrappers moved to VersionService,
        # async __getattr__ magic deleted in PR #2782)
        # ReBACService delegation (8 methods)
        "arebac_create",  # Delegates to rebac_service.rebac_create()
        "arebac_delete",  # Delegates to rebac_service.rebac_delete()
        "arebac_check",  # Delegates to rebac_service.rebac_check()
        "arebac_check_batch",  # Delegates to rebac_service.rebac_check_batch()
        "arebac_expand",  # Delegates to rebac_service.rebac_expand()
        "arebac_explain",  # Delegates to rebac_service.rebac_explain()
        "arebac_list_tuples",  # Delegates to rebac_service.rebac_list_tuples()
        "aget_namespace",  # Delegates to rebac_service.get_namespace()
        # MountService delegation (15 methods)
        "aadd_mount",  # Delegates to mount_service.add_mount()
        "aremove_mount",  # Delegates to mount_service.remove_mount()
        "alist_mounts",  # Delegates to mount_service.list_mounts()
        "aget_mount",  # Delegates to mount_service.get_mount()
        "ahas_mount",  # Delegates to mount_service.has_mount()
        "alist_connectors",  # Delegates to mount_service.list_connectors()
        "asave_mount",  # Delegates to mount_service.save_mount()
        "aload_mount",  # Delegates to mount_service.load_mount()
        "adelete_saved_mount",  # Delegates to mount_service.delete_saved_mount()
        "alist_saved_mounts",  # Delegates to mount_service.list_saved_mounts()
        "async_mount",  # Delegates to mount_service.sync_mount()
        "async_mount_async",  # Delegates to mount_service.sync_mount_async()
        "aget_sync_job",  # Delegates to mount_service.get_sync_job()
        "alist_sync_jobs",  # Delegates to mount_service.list_sync_jobs()
        "acancel_sync_job",  # Delegates to mount_service.cancel_sync_job()
        # SearchService delegation (4 methods)
        "asemantic_search",  # Delegates to search_service.semantic_search()
        "asemantic_search_index",  # Delegates to search_service.semantic_search_index()
        "asemantic_search_stats",  # Delegates to search_service.semantic_search_stats()
        # ainitialize_semantic_search — deleted from NexusFS, callers use search_service directly
        # Distributed Lock methods - async context managers require special handling
        # Tracked in Issue #1141
        "atomic_update",  # Async - read-modify-write with distributed lock
        # Async context manager - distributed lock acquisition
        # Consistency migration - server-side orchestration only (Issue #1180)
        "migrate_consistency_mode",  # Internal - SC↔EC migration orchestrator, exposed via PATCH endpoint
        # DispatchMixin methods (collapsed from KernelDispatch, PR 7c) — server-side only
        "register_observe",  # Internal - registers VFS observers
        "unregister_observe",  # Internal - unregisters VFS observers
        "register_resolver",  # Internal - registers PRE-DISPATCH resolvers
        "unregister_resolver",  # Internal - unregisters PRE-DISPATCH resolvers
        "register_intercept",  # Internal - generic hook registration
        "register_intercept_read",  # Internal - INTERCEPT hook registration
        "register_intercept_write",  # Internal - INTERCEPT hook registration
        "register_intercept_write_batch",  # Internal - INTERCEPT hook registration
        "register_intercept_delete",  # Internal - INTERCEPT hook registration
        "register_intercept_rename",  # Internal - INTERCEPT hook registration
        "register_intercept_copy",  # Internal - INTERCEPT hook registration
        "register_intercept_mkdir",  # Internal - INTERCEPT hook registration
        "register_intercept_rmdir",  # Internal - INTERCEPT hook registration
        "register_intercept_stat",  # Internal - INTERCEPT hook registration
        "register_intercept_access",  # Internal - INTERCEPT hook registration
        "unregister_intercept",  # Internal - generic hook unregistration
        "unregister_intercept_read",  # Internal - INTERCEPT hook unregistration
        "unregister_intercept_write",  # Internal - INTERCEPT hook unregistration
        "unregister_intercept_write_batch",  # Internal - INTERCEPT hook unregistration
        "unregister_intercept_delete",  # Internal - INTERCEPT hook unregistration
        "unregister_intercept_rename",  # Internal - INTERCEPT hook unregistration
        "unregister_intercept_copy",  # Internal - INTERCEPT hook unregistration
        "unregister_intercept_mkdir",  # Internal - INTERCEPT hook unregistration
        "unregister_intercept_rmdir",  # Internal - INTERCEPT hook unregistration
        "unregister_intercept_stat",  # Internal - INTERCEPT hook unregistration
        "unregister_intercept_access",  # Internal - INTERCEPT hook unregistration
        "dispatch_event",  # Internal - Rust kernel event dispatch
        "resolve_read",  # Internal - PRE-DISPATCH resolver
        "resolve_write",  # Internal - PRE-DISPATCH resolver
        "resolve_delete",  # Internal - PRE-DISPATCH resolver
        "intercept_pre_read",  # Internal - PRE-INTERCEPT dispatch
        "intercept_pre_write",  # Internal - PRE-INTERCEPT dispatch
        "intercept_pre_delete",  # Internal - PRE-INTERCEPT dispatch
        "intercept_pre_rename",  # Internal - PRE-INTERCEPT dispatch
        "intercept_pre_copy",  # Internal - PRE-INTERCEPT dispatch
        "intercept_pre_mkdir",  # Internal - PRE-INTERCEPT dispatch
        "intercept_pre_rmdir",  # Internal - PRE-INTERCEPT dispatch
        "intercept_pre_stat",  # Internal - PRE-INTERCEPT dispatch
        "intercept_pre_access",  # Internal - PRE-INTERCEPT dispatch
        # POST-INTERCEPT dispatch deleted — now via Rust dispatch_post_hooks
        "notify",  # Internal - OBSERVE dispatch
        "has_hooks",  # Internal - hook existence check
        "shutdown",  # Internal - background task drain
        # ABC compliance stubs (Issue #2033 LEGO decomposition)
        # These delegate to extracted services which already have @rpc_expose.
        # NexusFS defines them only to satisfy NexusFS ABC requirements.
        # Workspace snapshots — delegates to _workspace_rpc_service
        "workspace_snapshot",  # ABC stub → _workspace_rpc_service.workspace_snapshot()
        "workspace_restore",  # ABC stub → _workspace_rpc_service.workspace_restore()
        "workspace_log",  # ABC stub → _workspace_rpc_service.workspace_log()
        "workspace_diff",  # ABC stub → _workspace_rpc_service.workspace_diff()
        # Workspace registry — delegates to _workspace_rpc_service
        "register_workspace",  # ABC stub → _workspace_rpc_service.register_workspace()
        "unregister_workspace",  # ABC stub → _workspace_rpc_service.unregister_workspace()
        "list_workspaces",  # ABC stub → _workspace_rpc_service.list_workspaces()
        "get_workspace_info",  # ABC stub → _workspace_rpc_service.get_workspace_info()
        # Sandbox — delegates to _sandbox_rpc_service
        "sandbox_create",  # ABC stub → _sandbox_rpc_service.sandbox_create()
        "sandbox_get_or_create",  # ABC stub → _sandbox_rpc_service.sandbox_get_or_create()
        "sandbox_run",  # ABC stub → _sandbox_rpc_service.sandbox_run()
        "sandbox_pause",  # ABC stub → _sandbox_rpc_service.sandbox_pause()
        "sandbox_resume",  # ABC stub → _sandbox_rpc_service.sandbox_resume()
        "sandbox_stop",  # ABC stub → _sandbox_rpc_service.sandbox_stop()
        "sandbox_list",  # ABC stub → _sandbox_rpc_service.sandbox_list()
        "sandbox_status",  # ABC stub → _sandbox_rpc_service.sandbox_status()
        "sandbox_connect",  # ABC stub → _sandbox_rpc_service.sandbox_connect()
        "sandbox_disconnect",  # ABC stub → _sandbox_rpc_service.sandbox_disconnect()
        # Mount — delegates to mount_service (sync accessors)
        "add_mount",  # ABC stub → mount_service.add_mount_sync()
        "remove_mount",  # ABC stub → mount_service.remove_mount_sync()
        "list_mounts",  # ABC stub → mount_service.list_mounts_sync()
        "get_mount",  # ABC stub → mount_service.get_mount_sync()
        # Tier 2 convenience wrappers — delegate to Tier 1 sys_* which are already @rpc_expose
        "mkdir",  # Tier 2 → mkdir(parents=True, exist_ok=True)
        "rmdir",  # Tier 2 → sys_unlink(recursive=True)
        # Tier 2 IPC pipe/stream sync passthroughs (PR #3671) — local-only kernel
        # convenience methods. Remote callers go through sys_setattr / sys_read /
        # sys_write / sys_unlink which are already @rpc_expose. Sync wrappers
        # exist for tight in-process polling loops (audit drain, dedup queue,
        # LLM token pump) where async wrapping adds event-loop ping-pong.
        "pipe_create",  # Tier 2 → kernel.create_pipe (local-only)
        "pipe_close",  # Tier 2 → kernel.close_pipe (local-only)
        "has_pipe",  # Tier 2 → kernel.has_pipe (local-only)
        "stream_create",  # Tier 2 → kernel.create_stream (local-only)
        "stream_close",  # Tier 2 → kernel.close_stream (local-only)
        "stream_destroy",  # Tier 2 → kernel.destroy_stream (local-only)
        "stream_read_at",  # Tier 2 → kernel.stream_read_at (local-only)
        "stream_read_at_blocking",  # Tier 2 → kernel.stream_read_at_blocking (local-only)
        "stream_write_nowait",  # Tier 2 → kernel.stream_write_nowait (local-only)
        "has_stream",  # Tier 2 → kernel.has_stream (local-only)
        "stream_collect_all",  # Tier 2 → kernel.stream_collect_all (local-only)
        # Search/list — delegates to search_service
        "list",  # ABC stub → overrides NexusFS.list()
        "glob",  # ABC stub → search_service.glob()
        "grep",  # ABC stub → search_service.grep()
        # ReBAC sync delegation stubs (Issue #2033) — delegates to rebac_service
        "rebac_create",  # ABC stub → rebac_service.rebac_create_sync()
        "rebac_check",  # ABC stub → rebac_service.rebac_check_sync()
        "rebac_check_batch",  # ABC stub → rebac_service.rebac_check_batch_sync()
        "rebac_delete",  # ABC stub → rebac_service.rebac_delete_sync()
        "rebac_list_tuples",  # ABC stub → rebac_service.rebac_list_tuples_sync()
    }

    # Get all public methods
    all_methods = []
    for name in dir(NexusFS):
        if name.startswith("_"):
            continue
        try:
            attr = getattr(NexusFS, name)
            if callable(attr) and not isinstance(attr, type):
                all_methods.append(name)
        except Exception:
            continue

    # Get exposed methods
    exposed_methods = get_rpc_exposed_methods(NexusFS)

    # Find methods that are neither exposed nor in exclusion list
    not_exposed = set(all_methods) - set(exposed_methods.keys()) - INTERNAL_ONLY_METHODS

    if not_exposed:
        msg_lines = [
            "ENFORCEMENT FAILURE: The following public methods are NOT @rpc_expose decorated",
            "   and NOT in the INTERNAL_ONLY_METHODS exclusion list:",
            "",
        ]
        for name in sorted(not_exposed):
            try:
                method = getattr(NexusFS, name)
                doc = (inspect.getdoc(method) or "No docstring").split("\n")[0][:60]

                # Try to get source location
                try:
                    source_file = inspect.getsourcefile(method)
                    source_line = inspect.getsourcelines(method)[1]
                    location = f"{Path(source_file).name}:{source_line}"
                except Exception:
                    location = "unknown"

                msg_lines.append(f"  - {name}() [{location}]")
                msg_lines.append(f"    {doc}")
            except Exception:
                msg_lines.append(f"  - {name}()")

        msg_lines.extend(
            [
                "",
                "To fix this, you MUST do ONE of the following:",
                "",
                "1. Add @rpc_expose decorator to the method (RECOMMENDED)",
                "2. Add to INTERNAL_ONLY_METHODS if this should NOT be exposed",
                "",
                "Summary:",
                f"  Total public methods: {len(all_methods)}",
                f"  RPC exposed: {len(exposed_methods)}",
                f"  Internal-only (excluded): {len(INTERNAL_ONLY_METHODS)}",
                f"  Missing exposure: {len(not_exposed)}",
            ]
        )

        pytest.fail("\n".join(msg_lines))

    # Success
    print(f"\n✓ All {len(all_methods)} public methods are properly handled:")
    print(f"  - {len(exposed_methods)} exposed via @rpc_expose")
    print(f"  - {len(INTERNAL_ONLY_METHODS)} explicitly excluded (internal-only)")
    print("  - 0 missing (enforcement passed!)")


def test_no_exposed_method_starts_with_underscore():
    """Issue #2136: No exposed method name should start with '_'.

    This ensures that even if a method has _rpc_name set to something
    starting with '_', it won't bypass the discovery filter.
    """
    exposed_methods = get_all_rpc_exposed_methods()
    private_methods = [name for name in exposed_methods if name.startswith("_")]
    assert not private_methods, f"Exposed methods must not start with '_': {private_methods}"


def test_list_all_exposed_methods():
    """List all @rpc_expose methods for documentation purposes."""
    exposed_methods = get_all_rpc_exposed_methods()

    print(f"\n{'=' * 60}")
    print(f"All @rpc_expose methods ({len(exposed_methods)} total):")
    print(f"{'=' * 60}")

    # Group by category (rough heuristic based on method name)
    categories = {
        "File Operations": ["read", "write", "delete", "rename", "exists"],
        "Directory Operations": ["mkdir", "rmdir", "is_directory"],
        "Search/Query": ["list", "glob", "grep"],
        "Permissions (ReBAC)": ["rebac_create", "rebac_check", "rebac_delete", "rebac_expand"],
        "Versions": ["get_version", "list_versions", "rollback", "diff_versions"],
        "Workspace": ["workspace_snapshot", "workspace_restore", "workspace_log", "workspace_diff"],
        "Batch/Import/Export": [
            "write_batch",
            "batch_get_content_ids",
        ],
        "Other": [],
    }

    # Categorize methods
    categorized = {cat: [] for cat in categories}
    for name in sorted(exposed_methods.keys()):
        found = False
        for category, keywords in categories.items():
            if category == "Other":
                continue
            if any(kw in name for kw in keywords):
                categorized[category].append(name)
                found = True
                break
        if not found:
            categorized["Other"].append(name)

    # Print by category
    for category, methods in categorized.items():
        if methods:
            print(f"\n{category}:")
            for method in methods:
                desc = getattr(exposed_methods[method], "_rpc_description", "")
                desc_short = (desc or "").split("\n")[0][:50]
                print(f"  - {method}() {f'- {desc_short}' if desc_short else ''}")

    print(f"\n{'=' * 60}\n")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
