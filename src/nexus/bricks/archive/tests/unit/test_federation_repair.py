"""Tests for federation re-pair messaging on restore."""

from unittest.mock import MagicMock

from nexus.bricks.archive.cli_glue import _print_federation_repair_list


def test_prints_federation_urls(capsys):
    """Test that federation URLs are printed when list_federations returns data."""
    fs = MagicMock()
    fed_a = MagicMock()
    fed_a.url = "https://hub.example.com"
    fed_b = MagicMock()
    fed_b.url = "https://other.example.com"
    fs.metadata.list_federations.return_value = [fed_a, fed_b]
    _print_federation_repair_list(fs)
    captured = capsys.readouterr()
    assert "Federation re-pair required" in captured.out
    assert "nexus federation auth https://hub.example.com" in captured.out
    assert "nexus federation auth https://other.example.com" in captured.out


def test_silent_when_no_federations(capsys):
    """Test that nothing is printed when list_federations returns an empty list."""
    fs = MagicMock()
    fs.metadata.list_federations.return_value = []
    _print_federation_repair_list(fs)
    captured = capsys.readouterr()
    assert captured.out == ""


def test_silent_when_no_federation_api(capsys):
    """Test that nothing is printed when fs lacks metadata.list_federations."""
    fs = MagicMock(spec=[])
    _print_federation_repair_list(fs)
    captured = capsys.readouterr()
    assert captured.out == ""
