"""Unit tests for path normalization fix in workspace_registry and hierarchy_manager.

This test suite verifies that path normalization is consistent across:
- Workspace registration (workspace_registry.py)
- Memory registration (workspace_registry.py)
- Parent tuple creation (hierarchy_manager.py)
- Permission checks (permissions_enhanced.py)

Bug: Workspace registration created tuples with leading slashes (file:/workspace)
     but permission checks looked for paths without leading slashes (file:workspace),
     causing permission denials even when permissions were correctly granted.

Fix: Normalize paths by stripping leading slashes before creating ReBAC tuples,
     ensuring consistency with permission check behavior.
"""

import pytest
from sqlalchemy import create_engine

from nexus.core.hierarchy_manager import HierarchyManager
from nexus.core.rebac import NamespaceConfig
from nexus.core.rebac_manager import ReBACManager
from nexus.core.workspace_registry import WorkspaceRegistry
from nexus.storage.models import Base


@pytest.fixture
def engine():
    """Create in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def rebac_manager(engine):
    """Create a ReBAC manager for testing."""
    # Register file namespace for testing
    file_namespace = NamespaceConfig(
        namespace_id="file-ns",
        object_type="file",
        config={
            "relations": {
                "owner": {"union": ["direct_owner", "parent_owner"]},
                "editor": {"union": ["direct_editor", "parent_editor"]},
                "viewer": {"union": ["direct_viewer", "parent_viewer", "editor", "owner"]},
                "direct_owner": {},
                "direct_editor": {},
                "direct_viewer": {},
                "parent": {},
                "parent_owner": {
                    "tupleToUserset": {"tupleset": "parent", "computedUserset": "owner"}
                },
                "parent_editor": {
                    "tupleToUserset": {"tupleset": "parent", "computedUserset": "editor"}
                },
                "parent_viewer": {
                    "tupleToUserset": {"tupleset": "parent", "computedUserset": "viewer"}
                },
            }
        },
    )

    manager = ReBACManager(engine=engine, cache_ttl_seconds=300, max_depth=10)
    manager.create_namespace(file_namespace)

    yield manager
    manager.close()


@pytest.fixture
def workspace_registry(engine, rebac_manager):
    """Create a workspace registry for testing."""
    from nexus.storage.metadata_store import SQLAlchemyMetadataStore

    # Create metadata store - tables already created by engine fixture
    metadata_store = SQLAlchemyMetadataStore.__new__(SQLAlchemyMetadataStore)
    metadata_store.engine = engine

    return WorkspaceRegistry(metadata=metadata_store, rebac_manager=rebac_manager)


@pytest.fixture
def hierarchy_manager(rebac_manager):
    """Create a hierarchy manager for testing."""
    return HierarchyManager(rebac_manager=rebac_manager, enable_inheritance=True)


# ============================================================
# Workspace Registry Tests
# ============================================================


def test_workspace_registration_normalizes_path(workspace_registry, rebac_manager):
    """Test that workspace registration creates tuples without leading slashes."""
    # Register workspace with leading slash
    workspace_registry.register_workspace(
        path="/test_workspace",
        user_id="alice",
        agent_id=None,
        tenant_id="default",
    )

    # Verify permission works with normalized path (no leading slash)
    # This verifies the tuple was created with normalized path
    has_permission = rebac_manager.rebac_check(
        subject=("user", "alice"),
        permission="write",
        object=("file", "test_workspace"),  # No leading slash
        tenant_id="default",
    )

    assert has_permission, (
        "User should have write permission on workspace (verifies tuple created with normalized path)"
    )


def test_workspace_permission_check_works(workspace_registry, rebac_manager):
    """Test that permission checks work after workspace registration."""
    # Register workspace
    workspace_registry.register_workspace(
        path="/my_workspace",
        user_id="bob",
        agent_id=None,
        tenant_id="default",
    )

    # Check permission using path WITHOUT leading slash (as permission checks do)
    has_permission = rebac_manager.rebac_check(
        subject=("user", "bob"),
        permission="write",
        object=("file", "my_workspace"),  # No leading slash
        tenant_id="default",
    )

    assert has_permission, "User should have write permission on their workspace"


def test_memory_registration_normalizes_path(workspace_registry, rebac_manager):
    """Test that memory registration creates tuples without leading slashes."""
    # Register memory with leading slash
    workspace_registry.register_memory(
        path="/test_memory",
        user_id="charlie",
        agent_id=None,
        tenant_id="default",
    )

    # Verify permission works with normalized path (no leading slash)
    has_permission = rebac_manager.rebac_check(
        subject=("user", "charlie"),
        permission="write",
        object=("file", "test_memory"),  # No leading slash
        tenant_id="default",
    )

    assert has_permission, (
        "User should have write permission on memory (verifies tuple created with normalized path)"
    )


# ============================================================
# Hierarchy Manager Tests
# ============================================================


def test_parent_tuples_normalized_paths(hierarchy_manager, rebac_manager):
    """Test that parent tuples are created with normalized paths (no leading slash)."""
    # Grant permission on parent directory (without leading slash)
    rebac_manager.rebac_write(
        subject=("user", "test_user"),
        relation="direct_owner",
        object=("file", "workspace"),  # No leading slash
        tenant_id="default",
    )

    # Create parent tuples for a nested path
    created = hierarchy_manager.ensure_parent_tuples(
        path="/workspace/subdir/file.txt",
        tenant_id="default",
    )

    assert created > 0, "Should have created parent tuples"

    # Verify inherited permission works (proves tuples have normalized paths)
    has_permission = rebac_manager.rebac_check(
        subject=("user", "test_user"),
        permission="write",
        object=("file", "workspace/subdir/file.txt"),  # No leading slash
        tenant_id="default",
    )

    assert has_permission, "User should have inherited permission through parent tuples"


def test_parent_tuple_hierarchy_structure(hierarchy_manager, rebac_manager):
    """Test that parent tuple hierarchy is created correctly with normalized paths."""
    # Grant permission on top-level directory
    rebac_manager.rebac_write(
        subject=("user", "test_user2"),
        relation="direct_owner",
        object=("file", "a"),  # No leading slash
        tenant_id="default",
    )

    # Create parent tuples
    created = hierarchy_manager.ensure_parent_tuples(
        path="/a/b/c.txt",
        tenant_id="default",
    )

    assert created > 0, "Should have created parent tuples"

    # Verify nested file inherits permission (proves hierarchy structure is correct)
    has_permission = rebac_manager.rebac_check(
        subject=("user", "test_user2"),
        permission="write",
        object=("file", "a/b/c.txt"),  # Deeply nested file
        tenant_id="default",
    )

    assert has_permission, "User should have inherited permission on deeply nested file"


def test_parent_tuple_permission_inheritance(hierarchy_manager, rebac_manager):
    """Test that parent tuples enable permission inheritance."""
    # Grant permission on parent directory (without leading slash)
    rebac_manager.rebac_write(
        subject=("user", "david"),
        relation="direct_owner",
        object=("file", "parent_dir"),  # No leading slash
        tenant_id="default",
    )

    # Create parent tuple for child file
    hierarchy_manager.ensure_parent_tuples(
        path="/parent_dir/child.txt",
        tenant_id="default",
    )

    # Check that user has permission on child through parent
    has_permission = rebac_manager.rebac_check(
        subject=("user", "david"),
        permission="write",
        object=("file", "parent_dir/child.txt"),  # No leading slash
        tenant_id="default",
    )

    assert has_permission, "User should have inherited permission on child file"


# ============================================================
# Integration Tests
# ============================================================


def test_end_to_end_workspace_with_files(workspace_registry, hierarchy_manager, rebac_manager):
    """End-to-end test: register workspace, create files, verify permissions."""
    # 1. Register workspace
    workspace_registry.register_workspace(
        path="/project",
        user_id="eve",
        agent_id=None,
        tenant_id="default",
    )

    # 2. Create parent tuples for a file in the workspace
    hierarchy_manager.ensure_parent_tuples(
        path="/project/data/results.csv",
        tenant_id="default",
    )

    # 3. Verify user has permission on workspace
    has_workspace_perm = rebac_manager.rebac_check(
        subject=("user", "eve"),
        permission="write",
        object=("file", "project"),  # No leading slash
        tenant_id="default",
    )
    assert has_workspace_perm, "User should have permission on workspace"

    # 4. Verify user has inherited permission on nested file
    has_file_perm = rebac_manager.rebac_check(
        subject=("user", "eve"),
        permission="write",
        object=("file", "project/data/results.csv"),  # No leading slash
        tenant_id="default",
    )
    assert has_file_perm, "User should have inherited permission on nested file"


def test_no_leading_slash_in_any_tuple(workspace_registry, hierarchy_manager, rebac_manager):
    """Comprehensive test to ensure NO leading slashes in any tuples."""
    # Register workspace
    workspace_registry.register_workspace(path="/workspace1", user_id="frank", tenant_id="default")

    # Register memory
    workspace_registry.register_memory(path="/memory1", user_id="frank", tenant_id="default")

    # Create parent tuples
    hierarchy_manager.ensure_parent_tuples(path="/workspace1/dir/file.txt", tenant_id="default")

    # Verify all permissions work with normalized paths (no leading slashes)
    # This proves all tuples were created with normalized paths

    assert rebac_manager.rebac_check(
        subject=("user", "frank"),
        permission="write",
        object=("file", "workspace1"),  # No slash
        tenant_id="default",
    ), "Workspace permission should work"

    assert rebac_manager.rebac_check(
        subject=("user", "frank"),
        permission="write",
        object=("file", "memory1"),  # No slash
        tenant_id="default",
    ), "Memory permission should work"

    assert rebac_manager.rebac_check(
        subject=("user", "frank"),
        permission="write",
        object=("file", "workspace1/dir/file.txt"),  # No slash
        tenant_id="default",
    ), "Nested file permission should work through parent tuples"


# ============================================================
# Regression Tests (verify old bug is fixed)
# ============================================================


def test_regression_workspace_write_permission(workspace_registry, rebac_manager):
    """Regression test: verify workspace registration grants write permission.

    This was the original bug - workspace registration created tuple with leading slash,
    but permission check looked for path without leading slash, causing permission denial.
    """
    # Register workspace (this creates tuple with user as owner)
    workspace_registry.register_workspace(
        path="/test_regression",
        user_id="grace",
        tenant_id="default",
    )

    # THIS should work now (it failed before the fix)
    has_permission = rebac_manager.rebac_check(
        subject=("user", "grace"),
        permission="write",
        object=("file", "test_regression"),  # Permission check uses no leading slash
        tenant_id="default",
    )

    assert has_permission, (
        "REGRESSION: User should have write permission after workspace registration"
    )


def test_regression_read_after_write(workspace_registry, hierarchy_manager, rebac_manager):
    """Regression test: verify read permission works after write with parent tuples.

    Original bug caused read failures even though write succeeded.
    """
    # Register workspace
    workspace_registry.register_workspace(
        path="/read_test",
        user_id="henry",
        tenant_id="default",
    )

    # Create parent tuples (as write operation does)
    hierarchy_manager.ensure_parent_tuples(
        path="/read_test/file.txt",
        tenant_id="default",
    )

    # Check read permission (this failed before fix due to path mismatch)
    has_read_perm = rebac_manager.rebac_check(
        subject=("user", "henry"),
        permission="read",
        object=("file", "read_test/file.txt"),
        tenant_id="default",
    )

    assert has_read_perm, "REGRESSION: User should have read permission on their file"
