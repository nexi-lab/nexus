"""Tests for Rust-accelerated path_utils functions.

Validates that the Rust implementations in ``nexus_kernel`` produce identical
results to the Python fallback implementations in ``nexus.core.path_utils``.

Skipped when Rust extension is not available (pure-Python CI).
"""

from __future__ import annotations

import unittest

from nexus.contracts.constants import ROOT_ZONE_ID

try:
    from nexus_kernel import (
        canonicalize_path as rust_canonicalize_path,
    )
    from nexus_kernel import (
        extract_zone_id as rust_extract_zone_id,
    )
    from nexus_kernel import (
        get_ancestors as rust_get_ancestors,
    )
    from nexus_kernel import (
        get_parent as rust_get_parent,
    )
    from nexus_kernel import (
        get_parent_chain as rust_get_parent_chain,
    )
    from nexus_kernel import (
        normalize_path as rust_normalize_path,
    )
    from nexus_kernel import (
        parent_path as rust_parent_path,
    )
    from nexus_kernel import (
        path_matches_pattern as rust_path_matches_pattern,
    )
    from nexus_kernel import (
        split_path as rust_split_path,
    )
    from nexus_kernel import (
        unscope_internal_path as rust_unscope_internal_path,
    )
    from nexus_kernel import (
        validate_path as rust_validate_path,
    )

    RUST_AVAILABLE = True
except ImportError:
    RUST_AVAILABLE = False


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_kernel extension not available")
class TestRustSplitPath(unittest.TestCase):
    def test_empty(self) -> None:
        assert rust_split_path("") == []
        assert rust_split_path("/") == []

    def test_basic(self) -> None:
        assert rust_split_path("/a/b/c.txt") == ["a", "b", "c.txt"]

    def test_single(self) -> None:
        assert rust_split_path("/foo") == ["foo"]

    def test_trailing_slash(self) -> None:
        assert rust_split_path("/a/b/") == ["a", "b"]


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_kernel extension not available")
class TestRustGetParent(unittest.TestCase):
    def test_deep(self) -> None:
        assert rust_get_parent("/a/b/c.txt") == "/a/b"

    def test_shallow(self) -> None:
        assert rust_get_parent("/a") == "/"

    def test_root(self) -> None:
        assert rust_get_parent("/") is None


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_kernel extension not available")
class TestRustGetAncestors(unittest.TestCase):
    def test_deep(self) -> None:
        assert rust_get_ancestors("/a/b/c.txt") == ["/a/b/c.txt", "/a/b", "/a"]

    def test_single(self) -> None:
        assert rust_get_ancestors("/a") == ["/a"]

    def test_root(self) -> None:
        assert rust_get_ancestors("/") == []


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_kernel extension not available")
class TestRustGetParentChain(unittest.TestCase):
    def test_deep(self) -> None:
        result = rust_get_parent_chain("/a/b/c.txt")
        assert result == [("/a/b/c.txt", "/a/b"), ("/a/b", "/a")]

    def test_single(self) -> None:
        assert rust_get_parent_chain("/a") == []


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_kernel extension not available")
class TestRustParentPath(unittest.TestCase):
    def test_deep(self) -> None:
        assert rust_parent_path("/a/b/c") == "/a/b"

    def test_shallow(self) -> None:
        assert rust_parent_path("/a") == "/"

    def test_root(self) -> None:
        assert rust_parent_path("/") is None


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_kernel extension not available")
class TestRustValidatePath(unittest.TestCase):
    def test_strip_whitespace(self) -> None:
        assert rust_validate_path("  /foo/bar  ", False) == "/foo/bar"

    def test_add_leading_slash(self) -> None:
        assert rust_validate_path("foo/bar", False) == "/foo/bar"

    def test_collapse_slashes(self) -> None:
        assert rust_validate_path("foo///bar", False) == "/foo/bar"

    def test_remove_trailing_slash(self) -> None:
        assert rust_validate_path("/foo/bar/", False) == "/foo/bar"

    def test_reject_empty(self) -> None:
        with self.assertRaises(ValueError):
            rust_validate_path("", False)
        with self.assertRaises(ValueError):
            rust_validate_path("  ", False)

    def test_reject_root_unless_allowed(self) -> None:
        with self.assertRaises(ValueError):
            rust_validate_path("/", False)
        assert rust_validate_path("/", True) == "/"

    def test_reject_null_byte(self) -> None:
        with self.assertRaises(ValueError):
            rust_validate_path("/a\0b", False)

    def test_reject_control_chars(self) -> None:
        with self.assertRaises(ValueError):
            rust_validate_path("/a\nb", False)
        with self.assertRaises(ValueError):
            rust_validate_path("/a\rb", False)
        with self.assertRaises(ValueError):
            rust_validate_path("/a\tb", False)

    def test_reject_dotdot(self) -> None:
        with self.assertRaises(ValueError):
            rust_validate_path("/a/../b", False)

    def test_reject_dot_path_component(self) -> None:
        with self.assertRaises(ValueError):
            rust_validate_path("/a/./b", False)

    def test_allows_dotdot_inside_filename_components(self) -> None:
        assert rust_validate_path("/a/file..txt", False) == "/a/file..txt"
        assert rust_validate_path("/a/..hidden/file", False) == "/a/..hidden/file"

    def test_reject_component_whitespace(self) -> None:
        with self.assertRaises(ValueError):
            rust_validate_path("/ a/b", False)


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_kernel extension not available")
class TestRustNormalizePath(unittest.TestCase):
    def test_basic(self) -> None:
        assert rust_normalize_path("/a//b/./c") == "/a/b/c"

    def test_dotdot(self) -> None:
        assert rust_normalize_path("/a/b/../c") == "/a/c"

    def test_root(self) -> None:
        assert rust_normalize_path("/") == "/"

    def test_reject_relative(self) -> None:
        with self.assertRaises(ValueError):
            rust_normalize_path("a/b")


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_kernel extension not available")
class TestRustPathMatchesPattern(unittest.TestCase):
    def test_star(self) -> None:
        assert rust_path_matches_pattern("/a/b.txt", "/a/*.txt") is True
        assert rust_path_matches_pattern("/a/b/c.txt", "/a/*.txt") is False

    def test_double_star(self) -> None:
        assert rust_path_matches_pattern("/a/b/c.txt", "/a/**/*.txt") is True

    def test_question_mark(self) -> None:
        assert rust_path_matches_pattern("/a/b", "/a/?") is True
        assert rust_path_matches_pattern("/a/bc", "/a/?") is False

    def test_exact(self) -> None:
        assert rust_path_matches_pattern("/a/b.txt", "/a/b.txt") is True
        assert rust_path_matches_pattern("/a/b.txt", "/a/c.txt") is False


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_kernel extension not available")
class TestRustUnscopeInternalPath(unittest.TestCase):
    def test_tenant(self) -> None:
        assert rust_unscope_internal_path("/tenant:x/workspace/f.txt") == "/workspace/f.txt"

    def test_tenant_user(self) -> None:
        assert rust_unscope_internal_path("/tenant:x/user:y/data/f.txt") == "/data/f.txt"

    def test_zone(self) -> None:
        assert rust_unscope_internal_path("/zone/alpha/workspace/f.txt") == "/workspace/f.txt"

    def test_zone_user(self) -> None:
        assert rust_unscope_internal_path("/zone/alpha/user:y/data/f.txt") == "/data/f.txt"

    def test_no_prefix(self) -> None:
        assert rust_unscope_internal_path("/workspace/f.txt") == "/workspace/f.txt"

    def test_root_result(self) -> None:
        assert rust_unscope_internal_path("/tenant:x") == "/"


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_kernel extension not available")
class TestRustCanonicalizePath(unittest.TestCase):
    def test_basic(self) -> None:
        assert rust_canonicalize_path("/workspace/file.txt", "root") == "/root/workspace/file.txt"

    def test_root(self) -> None:
        assert rust_canonicalize_path("/", "root") == "/root"

    def test_custom_zone(self) -> None:
        assert rust_canonicalize_path("/a/b/c", "zone-1") == "/zone-1/a/b/c"

    def test_default_zone(self) -> None:
        # Default zone_id is "root"
        assert rust_canonicalize_path("/workspace") == "/root/workspace"


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_kernel extension not available")
class TestRustExtractZoneId(unittest.TestCase):
    def test_basic(self) -> None:
        zone, relative = rust_extract_zone_id("/root/workspace/file.txt")
        assert zone == ROOT_ZONE_ID
        assert relative == "/workspace/file.txt"

    def test_root_only(self) -> None:
        zone, relative = rust_extract_zone_id("/root")
        assert zone == ROOT_ZONE_ID
        assert relative == "/"

    def test_custom_zone(self) -> None:
        zone, relative = rust_extract_zone_id("/zone-1/a/b")
        assert zone == "zone-1"
        assert relative == "/a/b"


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_kernel extension not available")
class TestRustPythonParity(unittest.TestCase):
    """Cross-check: Rust and Python produce identical results."""

    def test_split_path_parity(self) -> None:
        # Force Python fallback for comparison
        cases = ["/", "", "/a/b/c", "/foo", "/a/b/"]
        for p in cases:
            py_parts = tuple(p.strip("/").split("/")) if p and p != "/" else ()
            if p.endswith("/") and p != "/" and p:
                py_parts = tuple(p.strip("/").split("/"))
            rust_parts = tuple(rust_split_path(p))
            # Filter empty strings from Python
            py_parts = tuple(x for x in py_parts if x)
            assert rust_parts == py_parts, f"split_path({p!r}): Rust={rust_parts}, Py={py_parts}"

    def test_normalize_parity(self) -> None:
        import posixpath

        cases = ["/a//b/./c", "/a/b/../c", "/", "/a/b/c"]
        for p in cases:
            py_result = posixpath.normpath(p)
            rust_result = rust_normalize_path(p)
            assert rust_result == py_result, f"normalize({p!r}): Rust={rust_result}, Py={py_result}"

    def test_canonicalize_parity(self) -> None:
        cases = [("/workspace/file.txt", "root"), ("/", "root"), ("/a/b", "zone-1")]
        for path, zone in cases:
            stripped = path.lstrip("/")
            py = f"/{zone}/{stripped}" if stripped else f"/{zone}"
            rust = rust_canonicalize_path(path, zone)
            assert rust == py, f"canonicalize({path!r}, {zone!r}): Rust={rust}, Py={py}"

    def test_extract_zone_parity(self) -> None:
        cases = ["/root/workspace/file.txt", "/root", "/zone-1/a/b"]
        for c in cases:
            parts = c.lstrip("/").split("/", 1)
            py_zone = parts[0]
            py_rel = "/" + parts[1] if len(parts) > 1 else "/"
            rust_zone, rust_rel = rust_extract_zone_id(c)
            assert (rust_zone, rust_rel) == (py_zone, py_rel), (
                f"extract_zone_id({c!r}): Rust=({rust_zone}, {rust_rel}), Py=({py_zone}, {py_rel})"
            )


if __name__ == "__main__":
    unittest.main()
