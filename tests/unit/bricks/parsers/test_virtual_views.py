from __future__ import annotations

from nexus.lib.virtual_views import parse_virtual_path, should_add_virtual_views


def test_parse_virtual_path_preserves_unicode_prefix_and_uppercase_extension() -> None:
    checked_paths: list[str] = []

    def exists(path: str) -> bool:
        checked_paths.append(path)
        return path == "/İdir/Report.PDF"

    assert parse_virtual_path("/İdir/Report_parsed.PDF.md", exists) == (
        "/İdir/Report.PDF",
        "md",
        True,
    )
    assert checked_paths == ["/İdir/Report.PDF"]


def test_should_add_virtual_views_only_checks_parsed_suffix_in_filename() -> None:
    assert should_add_virtual_views("/dir_parsed.archive/Report.PDF") is True
    assert should_add_virtual_views("/dir/report_parsed.PDF.md") is False
