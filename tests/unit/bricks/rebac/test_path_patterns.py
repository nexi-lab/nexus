"""Path-pattern tuple helper tests for ReBAC issue #4239."""

from nexus.bricks.rebac.path_patterns import (
    is_path_pattern,
    path_pattern_candidates,
    path_pattern_matches,
    path_pattern_prefix,
)


def test_recursive_root_pattern_matches_all_absolute_paths() -> None:
    assert is_path_pattern("file", "/**")
    assert path_pattern_prefix("file", "/**") == "/"
    assert path_pattern_matches("/**", "/")
    assert path_pattern_matches("/**", "/a")
    assert path_pattern_matches("/**", "/a/b.txt")


def test_recursive_prefix_matches_prefix_and_descendants_only() -> None:
    assert is_path_pattern("file", "/workspaces/**")
    assert path_pattern_prefix("file", "/workspaces/**") == "/workspaces"
    assert path_pattern_matches("/workspaces/**", "/workspaces")
    assert path_pattern_matches("/workspaces/**", "/workspaces/ws1/a.md")
    assert not path_pattern_matches("/workspaces/**", "/workspaces2/a.md")


def test_single_level_pattern_matches_direct_children_only() -> None:
    assert is_path_pattern("file", "/workspaces/*")
    assert path_pattern_prefix("file", "/workspaces/*") == "/workspaces"
    assert path_pattern_matches("/workspaces/*", "/workspaces/a.md")
    assert not path_pattern_matches("/workspaces/*", "/workspaces")
    assert not path_pattern_matches("/workspaces/*", "/workspaces/a/b.md")
    assert not path_pattern_matches("/workspaces/*", "/workspaces2/a.md")


def test_root_single_level_pattern_matches_one_root_child() -> None:
    assert is_path_pattern("file", "/*")
    assert path_pattern_prefix("file", "/*") == "/"
    assert path_pattern_matches("/*", "/a.md")
    assert not path_pattern_matches("/*", "/")
    assert not path_pattern_matches("/*", "/a/b.md")


def test_non_file_and_relative_ids_are_not_patterns() -> None:
    assert not is_path_pattern("group", "/workspaces/**")
    assert not is_path_pattern("file", "workspaces/**")
    assert path_pattern_prefix("group", "/workspaces/**") is None
    assert path_pattern_candidates("group", "/workspaces/a.md") == ["/workspaces/a.md"]
    assert path_pattern_candidates("file", "workspaces/a.md") == ["workspaces/a.md"]


def test_candidates_are_bounded_and_ordered_from_exact_to_broad() -> None:
    assert path_pattern_candidates("file", "/workspaces/ws1/a.md") == [
        "/workspaces/ws1/a.md",
        "/workspaces/ws1/a.md/**",
        "/workspaces/ws1/*",
        "/workspaces/ws1/**",
        "/workspaces/**",
        "/**",
    ]
    assert path_pattern_candidates("file", "/a.md") == ["/a.md", "/a.md/**", "/*", "/**"]
    assert path_pattern_candidates("file", "/") == ["/", "/**"]
