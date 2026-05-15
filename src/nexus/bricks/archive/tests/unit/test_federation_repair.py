"""Tests for federation re-pair messaging on restore.

After commit V dropped the legacy ``list_federations`` Raft method, the
``_print_federation_repair_list`` helper is an unconditional no-op until
the kernel exposes a federation registry. These tests pin that
behaviour so a future kernel re-export doesn't silently regress the
operator-visible message.
"""

from unittest.mock import MagicMock

from nexus.bricks.archive.cli_glue import _print_federation_repair_list


def test_prints_nothing_when_kernel_lacks_federation_api(capsys):
    """No federation registry on the kernel ⇒ no repair output."""
    fs = MagicMock(spec=[])
    _print_federation_repair_list(fs)
    captured = capsys.readouterr()
    assert captured.out == ""


def test_prints_nothing_with_full_mock(capsys):
    """The helper is a no-op even when callers stub the legacy attribute."""
    fs = MagicMock()
    fs.metadata.list_federations.return_value = [MagicMock(url="https://x")]
    _print_federation_repair_list(fs)
    captured = capsys.readouterr()
    assert captured.out == ""
