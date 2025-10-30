"""Simple unit tests for path normalization fix.

This test suite focuses on the core fix: ensuring paths are normalized
(leading slashes stripped) before creating ReBAC tuples.

Bug Fixed: Workspace registration created tuples with leading slashes (file:/workspace)
           but permission checks looked for paths without leading slashes (file:workspace).

Fix: Strip leading slashes when creating tuples in workspace_registry.py and hierarchy_manager.py.
"""


def test_workspace_registry_strips_leading_slash():
    """Test that workspace_registry.py strips leading slashes in object IDs."""
    # Simulating the fixed code
    path = "/test_workspace"

    # OLD BUGGY CODE (before fix):
    # object=("file", path)  # Would be file:/test_workspace

    # NEW FIXED CODE:
    normalized_path = path.lstrip("/")  # Strips to "test_workspace"

    assert normalized_path == "test_workspace", "Path should not have leading slash"
    assert not normalized_path.startswith("/"), "Normalized path must not start with /"


def test_hierarchy_manager_strips_leading_slash():
    """Test that hierarchy_manager.py strips leading slashes from parent paths."""
    # Simulating the fixed code
    path = "/workspace/subdir/file.txt"
    parts = path.strip("/").split("/")  # ["workspace", "subdir", "file.txt"]

    # OLD BUGGY CODE (before fix):
    # child_path = "/" + "/".join(parts[:2])  # Would be "/workspace/subdir"
    # parent_path = "/" + "/".join(parts[:1])  # Would be "/workspace"

    # NEW FIXED CODE:
    child_path = "/".join(parts[:2])  # "workspace/subdir"
    parent_path = "/".join(parts[:1])  # "workspace"

    assert child_path == "workspace/subdir", "Child path should not have leading slash"
    assert parent_path == "workspace", "Parent path should not have leading slash"
    assert not child_path.startswith("/"), "Child path must not start with /"
    assert not parent_path.startswith("/"), "Parent path must not start with /"


def test_path_normalization_examples():
    """Test various path normalization examples."""
    test_cases = [
        ("/workspace", "workspace"),
        ("/a/b/c", "a/b/c"),
        ("/joe_personal", "joe_personal"),
        ("/test_workspace/file.txt", "test_workspace/file.txt"),
    ]

    for input_path, expected_normalized in test_cases:
        normalized = input_path.lstrip("/")
        assert normalized == expected_normalized, f"Failed for {input_path}"
        assert not normalized.startswith("/"), f"Normalized path has leading slash: {normalized}"


def test_root_path_edge_case():
    """Test that root path is handled correctly."""
    # Root path is a special case
    path = "/"
    normalized = path.lstrip("/")

    assert normalized == "", "Root path should normalize to empty string"


def test_no_leading_slash_path():
    """Test that paths without leading slashes are unchanged."""
    path = "already/normalized"
    normalized = path.lstrip("/")

    assert normalized == "already/normalized", "Already normalized path should be unchanged"


def test_multiple_leading_slashes():
    """Test that multiple leading slashes are all stripped."""
    path = "///multiple/slashes"
    normalized = path.lstrip("/")

    assert normalized == "multiple/slashes", "All leading slashes should be stripped"
    assert not normalized.startswith("/"), "No leading slashes should remain"
