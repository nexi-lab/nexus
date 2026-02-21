"""Unit tests for _scoped_base.py shared path-scoping logic (Phase 6.1, Issue #2033)."""

from nexus.core._scoped_base import GLOBAL_NAMESPACES, ScopedPathMixin


class _TestScoper(ScopedPathMixin):
    """Minimal concrete subclass for testing."""

    pass


class TestScopedPathMixin:
    def test_scope_regular_path(self):
        s = _TestScoper("/zones/t1/users/u1")
        assert s._scope_path("/workspace/file.txt") == "/zones/t1/users/u1/workspace/file.txt"

    def test_scope_root_path(self):
        s = _TestScoper("/zones/t1/users/u1")
        assert s._scope_path("/") == "/zones/t1/users/u1/"

    def test_scope_no_leading_slash(self):
        s = _TestScoper("/zones/t1/users/u1")
        assert s._scope_path("workspace/file.txt") == "/zones/t1/users/u1/workspace/file.txt"

    def test_scope_global_namespaces_passthrough(self):
        s = _TestScoper("/zones/t1/users/u1")
        for ns in GLOBAL_NAMESPACES:
            path = f"{ns}something"
            assert s._scope_path(path) == path

    def test_unscope_regular_path(self):
        s = _TestScoper("/zones/t1/users/u1")
        assert s._unscope_path("/zones/t1/users/u1/workspace/file.txt") == "/workspace/file.txt"

    def test_unscope_root_returns_slash(self):
        s = _TestScoper("/zones/t1/users/u1")
        assert s._unscope_path("/zones/t1/users/u1") == "/"

    def test_unscope_global_namespace_passthrough(self):
        s = _TestScoper("/zones/t1/users/u1")
        assert s._unscope_path("/skills/my_skill") == "/skills/my_skill"

    def test_unscope_non_matching_path(self):
        s = _TestScoper("/zones/t1/users/u1")
        assert s._unscope_path("/other/path") == "/other/path"

    def test_unscope_paths_batch(self):
        s = _TestScoper("/zones/t1/users/u1")
        paths = ["/zones/t1/users/u1/a.txt", "/zones/t1/users/u1/b.txt"]
        assert s._unscope_paths(paths) == ["/a.txt", "/b.txt"]

    def test_unscope_dict(self):
        s = _TestScoper("/zones/t1/users/u1")
        d = {"path": "/zones/t1/users/u1/file.txt", "size": 100}
        result = s._unscope_dict(d, ["path"])
        assert result["path"] == "/file.txt"
        assert result["size"] == 100
        # Original dict unchanged (immutable pattern)
        assert d["path"] == "/zones/t1/users/u1/file.txt"

    def test_empty_root(self):
        s = _TestScoper("")
        assert s._scope_path("/workspace/file.txt") == "/workspace/file.txt"
        assert s._unscope_path("/workspace/file.txt") == "/workspace/file.txt"

    def test_root_property(self):
        s = _TestScoper("/zones/t1")
        assert s.root == "/zones/t1"

    def test_root_normalization(self):
        s = _TestScoper("zones/t1/")
        assert s.root == "/zones/t1"
