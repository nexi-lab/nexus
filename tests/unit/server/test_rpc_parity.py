#!/usr/bin/env python3
"""Test that all @rpc_expose methods have corresponding RemoteNexusFS implementations.

This test ensures that whenever a method is decorated with @rpc_expose in the core
implementation, it also has a corresponding client method in RemoteNexusFS.

This prevents issues like #268 where methods are added to core but forgotten in remote client.
"""

import inspect
from pathlib import Path

import pytest

from nexus.core.nexus_fs import NexusFS
from nexus.remote.client import RemoteNexusFS


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


def get_remote_methods(cls):
    """Get all public methods from RemoteNexusFS.

    Returns:
        dict: Mapping of method name to method object
    """
    methods = {}
    for name in dir(cls):
        if name.startswith("_"):
            continue
        try:
            attr = getattr(cls, name)
            if callable(attr) and not isinstance(attr, type):
                methods[name] = attr
        except Exception:
            continue
    return methods


def test_all_rpc_methods_have_remote_implementations():
    """Verify all @rpc_expose methods have RemoteNexusFS implementations.

    This test ensures parity between local and remote implementations.
    If this test fails, it means a method was added with @rpc_expose but
    the corresponding client method was not added to RemoteNexusFS.
    """
    # Get all exposed RPC methods from core
    exposed_methods = get_rpc_exposed_methods(NexusFS)

    # Get all methods from RemoteNexusFS
    remote_methods = get_remote_methods(RemoteNexusFS)

    # Check which exposed methods are missing from remote
    missing_in_remote = []
    for rpc_name, method in exposed_methods.items():
        if rpc_name not in remote_methods:
            # Get the actual method name in case RPC name differs
            actual_name = method.__name__
            if actual_name not in remote_methods:
                missing_in_remote.append(rpc_name)

    # Report findings
    if missing_in_remote:
        msg_lines = [
            "The following @rpc_expose methods are missing from RemoteNexusFS:",
            "",
        ]
        for name in sorted(missing_in_remote):
            method = exposed_methods[name]
            # Try to get the source file location
            try:
                source_file = inspect.getsourcefile(method)
                source_line = inspect.getsourcelines(method)[1]
                location = f"{Path(source_file).name}:{source_line}"
            except Exception:
                location = "unknown"

            msg_lines.append(f"  - {name}() [{location}]")

        msg_lines.extend(
            [
                "",
                "To fix this:",
                "1. Add the missing method(s) to src/nexus/remote/client.py",
                "2. Each method should call self._call_rpc(method_name, params)",
                "3. See existing methods in RemoteNexusFS for examples",
                "",
                f"Total exposed methods: {len(exposed_methods)}",
                f"Total remote methods: {len(remote_methods)}",
                f"Missing: {len(missing_in_remote)}",
            ]
        )

        pytest.fail("\n".join(msg_lines))

    # Success - print summary for visibility
    print(f"\n✓ All {len(exposed_methods)} @rpc_expose methods have RemoteNexusFS implementations")
    print(f"  Exposed methods: {', '.join(sorted(exposed_methods.keys())[:5])}...")
    print(f"  Remote methods: {len(remote_methods)} total")


def test_remote_methods_match_signatures():
    """Verify RemoteNexusFS method signatures match core methods (where applicable).

    This is a best-effort check - signatures may differ slightly due to
    context parameters being handled server-side.
    """
    exposed_methods = get_rpc_exposed_methods(NexusFS)
    remote_methods = get_remote_methods(RemoteNexusFS)

    signature_mismatches = []

    for rpc_name, core_method in exposed_methods.items():
        if rpc_name not in remote_methods:
            continue  # Already caught by other test

        remote_method = remote_methods[rpc_name]

        try:
            core_sig = inspect.signature(core_method)
            remote_sig = inspect.signature(remote_method)

            # Get parameter names (excluding 'self' and 'context')
            core_params = [p for p in core_sig.parameters if p not in ("self", "context", "cls")]
            remote_params = [
                p for p in remote_sig.parameters if p not in ("self", "context", "cls")
            ]

            # Check if core parameters are present in remote (order may differ)
            missing_params = set(core_params) - set(remote_params)

            # Allow some common variations
            allowed_missing = {"context", "return_metadata"}
            missing_params = missing_params - allowed_missing

            if missing_params:
                signature_mismatches.append(
                    {
                        "method": rpc_name,
                        "core_params": core_params,
                        "remote_params": remote_params,
                        "missing": list(missing_params),
                    }
                )

        except Exception:
            # Skip signature comparison if introspection fails
            continue

    if signature_mismatches:
        msg_lines = ["Warning: Some method signatures may not match:"]
        for mismatch in signature_mismatches:
            msg_lines.append(f"  {mismatch['method']}():")
            msg_lines.append(f"    Missing params: {mismatch['missing']}")

        # Don't fail on signature mismatches (they may be intentional)
        # Just print a warning
        print("\n".join(msg_lines))
    else:
        print("\n✓ Method signatures look compatible")


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
        "close",  # Connection management - handled differently for remote
        "load_all_saved_mounts",  # Internal initialization method - called automatically on startup
        # Server-side only methods (clients get this via HTTP headers)
        "get_etag",  # Returns ETag for early 304 check - clients receive ETags via HTTP headers on read
        # Async methods - TODO: Add async RPC support
        # Tracked in issue #XXX
        "semantic_search",  # Async - requires async RPC support
        "semantic_search_index",  # Async - requires async RPC support
        "semantic_search_stats",  # Async - requires async RPC support
        "initialize_semantic_search",  # Async - requires async RPC support
        "parse",  # Async - requires async RPC support
        # Already exposed via different mechanism
        "write_batch",  # Exposed via different RPC endpoint
        "list_memories",  # Handled manually by dispatcher, calls memory.list() instead
        # Tiger Cache internal methods - server-side optimization only
        "grant_traverse_on_implicit_dirs",  # Internal - grants TRAVERSE on implicit dirs during init
        "process_tiger_cache_queue",  # Internal - background worker processes cache updates
        "stop_tiger_cache_worker",  # Internal - stops background worker thread
        "warm_tiger_cache",  # Internal - pre-computes permissions for cache warming
        # Directory Visibility Cache internal methods - server-side optimization only
        "clear_dir_visibility_cache",  # Internal - clears the directory visibility cache
        "get_dir_visibility_cache_metrics",  # Internal - returns cache metrics for monitoring
        # Phase 2 Service Composition - Async delegation methods (Issue #988)
        # These are internal async methods that delegate to services. The original
        # sync mixin methods (without "a" prefix) already have @rpc_expose decorators.
        # These will be removed when mixins are deprecated in Phase 2.3.
        # VersionService delegation (4 methods)
        "aget_version",  # Delegates to version_service.get_version()
        "alist_versions",  # Delegates to version_service.list_versions()
        "arollback",  # Delegates to version_service.rollback()
        "adiff_versions",  # Delegates to version_service.diff_versions()
        # ReBACService delegation (8 methods)
        "arebac_create",  # Delegates to rebac_service.rebac_create()
        "arebac_delete",  # Delegates to rebac_service.rebac_delete()
        "arebac_check",  # Delegates to rebac_service.rebac_check()
        "arebac_check_batch",  # Delegates to rebac_service.rebac_check_batch()
        "arebac_expand",  # Delegates to rebac_service.rebac_expand()
        "arebac_explain",  # Delegates to rebac_service.rebac_explain()
        "arebac_list_tuples",  # Delegates to rebac_service.rebac_list_tuples()
        "aget_namespace",  # Delegates to rebac_service.get_namespace()
        # MCPService delegation (5 methods)
        "amcp_mount",  # Delegates to mcp_service.mcp_mount()
        "amcp_unmount",  # Delegates to mcp_service.mcp_unmount()
        "amcp_list_mounts",  # Delegates to mcp_service.mcp_list_mounts()
        "amcp_list_tools",  # Delegates to mcp_service.mcp_list_tools()
        "amcp_sync",  # Delegates to mcp_service.mcp_sync()
        "amcp_connect",  # Delegates to mcp_service.mcp_connect()
        # LLMService delegation (4 methods)
        "acreate_llm_reader",  # Delegates to llm_service.create_llm_reader()
        "allm_read",  # Delegates to llm_service.llm_read()
        "allm_read_detailed",  # Delegates to llm_service.llm_read_detailed()
        "allm_read_stream",  # Delegates to llm_service.llm_read_stream()
        # OAuthService delegation (7 methods)
        "aoauth_list_providers",  # Delegates to oauth_service.oauth_list_providers()
        "aoauth_get_auth_url",  # Delegates to oauth_service.oauth_get_auth_url()
        "aoauth_exchange_code",  # Delegates to oauth_service.oauth_exchange_code()
        "aoauth_list_credentials",  # Delegates to oauth_service.oauth_list_credentials()
        "aoauth_revoke_credential",  # Delegates to oauth_service.oauth_revoke_credential()
        "aoauth_test_credential",  # Delegates to oauth_service.oauth_test_credential()
        # SkillService delegation (15 methods)
        "askills_list",  # Delegates to skill_service.skills_list()
        "askills_info",  # Delegates to skill_service.skills_info()
        "askills_search",  # Delegates to skill_service.skills_search()
        "askills_create",  # Delegates to skill_service.skills_create()
        "askills_publish",  # Delegates to skill_service.skills_publish()
        "askills_fork",  # Delegates to skill_service.skills_fork()
        "askills_validate_zip",  # Delegates to skill_service.skills_validate_zip()
        "askills_import",  # Delegates to skill_service.skills_import()
        "askills_submit_approval",  # Delegates to skill_service.skills_submit_approval()
        "askills_approve",  # Delegates to skill_service.skills_approve()
        "askills_reject",  # Delegates to skill_service.skills_reject()
        "askills_list_approvals",  # Delegates to skill_service.skills_list_approvals()
        "askills_export",  # Delegates to skill_service.skills_export()
        "askills_create_from_file",  # Delegates to skill_service.skills_create_from_file()
        "askills_create_from_content",  # Delegates to skill_service.skills_create_from_content()
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
        "ainitialize_semantic_search",  # Delegates to search_service.initialize_semantic_search()
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
            "❌ ENFORCEMENT FAILURE: The following public methods are NOT @rpc_expose decorated",
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
                "1. Add @rpc_expose decorator to the method (RECOMMENDED):",
                "   ```python",
                "   from nexus.core.rpc_decorator import rpc_expose",
                "",
                "   @rpc_expose(description='Your description')",
                "   def your_method(self, ...):",
                "       ...",
                "   ```",
                "",
                "2. Add RemoteNexusFS implementation:",
                "   - See docs/RPC_PARITY_GUIDE.md for instructions",
                "",
                "3. Add to INTERNAL_ONLY_METHODS if this should NOT be exposed:",
                "   - Edit tests/unit/test_rpc_parity.py",
                "   - Add method name to INTERNAL_ONLY_METHODS with justification",
                "   - This should be RARE - most methods should be exposed",
                "",
                "Summary:",
                f"  Total public methods: {len(all_methods)}",
                f"  RPC exposed: {len(exposed_methods)}",
                f"  Internal-only (excluded): {len(INTERNAL_ONLY_METHODS)}",
                f"  ❌ Missing exposure: {len(not_exposed)}",
            ]
        )

        pytest.fail("\n".join(msg_lines))

    # Success
    print(f"\n✓ All {len(all_methods)} public methods are properly handled:")
    print(f"  - {len(exposed_methods)} exposed via @rpc_expose")
    print(f"  - {len(INTERNAL_ONLY_METHODS)} explicitly excluded (internal-only)")
    print("  - 0 missing (enforcement passed!)")


def test_list_all_exposed_methods():
    """List all @rpc_expose methods for documentation purposes."""
    exposed_methods = get_rpc_exposed_methods(NexusFS)

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
            "export_metadata",
            "import_metadata",
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
