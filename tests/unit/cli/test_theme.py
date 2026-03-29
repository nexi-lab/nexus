"""Tests for centralized CLI theme (Issue #3241)."""

from __future__ import annotations

import io
from collections.abc import Callable

from rich.console import Console
from rich.style import Style

from nexus.cli.theme import (
    NEXUS_THEME,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNSET,
    STATUS_WARN,
    console,
    err_console,
    print_error,
    print_info,
    print_success,
    print_warning,
)

# All 12 semantic tokens that must be present in the theme.
EXPECTED_TOKENS = [
    "nexus.success",
    "nexus.warning",
    "nexus.error",
    "nexus.info",
    "nexus.accent",
    "nexus.muted",
    "nexus.hint",
    "nexus.path",
    "nexus.value",
    "nexus.label",
    "nexus.identity",
    "nexus.reference",
]


def _capture(c: Console, text: str) -> str:
    """Print *text* to console *c* and return the raw output."""
    with c.capture() as cap:
        c.print(text, end="")
    return cap.get()


def _make_console(**overrides: object) -> Console:
    """Create a test console with the nexus theme."""
    buf = io.StringIO()
    defaults: dict[str, object] = {
        "theme": NEXUS_THEME,
        "file": buf,
        "force_terminal": True,
        "width": 120,
        "color_system": "truecolor",
    }
    defaults.update(overrides)
    return Console(**defaults)


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------


class TestTokenResolution:
    """Every semantic token resolves to a valid Rich Style."""

    def test_all_tokens_present(self) -> None:
        for token in EXPECTED_TOKENS:
            style = NEXUS_THEME.styles.get(token)
            assert style is not None, f"Missing token: {token}"

    def test_all_tokens_are_style_objects(self) -> None:
        for token in EXPECTED_TOKENS:
            style = NEXUS_THEME.styles[token]
            assert isinstance(style, Style), f"{token} is not a Style: {type(style)}"

    def test_no_extra_tokens(self) -> None:
        """Theme should not contain unexpected non-nexus tokens beyond Rich builtins."""
        # Rich's Theme(inherit=True) merges in built-in styles like "none", "reset", etc.
        # We only check that all 12 nexus tokens are present (tested above).
        custom_tokens = [n for n in NEXUS_THEME.styles if n.startswith("nexus.")]
        assert len(custom_tokens) == len(EXPECTED_TOKENS)


# ---------------------------------------------------------------------------
# Console singletons
# ---------------------------------------------------------------------------


class TestConsoleSingletons:
    """Shared console instances are correctly configured."""

    def test_console_has_theme(self) -> None:
        # Console should resolve our custom tokens without error.
        style = console.get_style("nexus.error")
        assert style is not None

    def test_err_console_writes_to_stderr(self) -> None:
        # err_console should target stderr and have the theme.
        assert err_console.stderr is True
        style = err_console.get_style("nexus.error")
        assert style is not None


# ---------------------------------------------------------------------------
# Status icon constants
# ---------------------------------------------------------------------------


class TestStatusConstants:
    """Status icons render correctly through the theme."""

    def test_status_ok_contains_checkmark(self) -> None:
        assert "\u2713" in _capture(_make_console(), STATUS_OK)

    def test_status_error_contains_cross(self) -> None:
        assert "\u2717" in _capture(_make_console(), STATUS_ERROR)

    def test_status_warn_contains_warning_sign(self) -> None:
        assert "\u26a0" in _capture(_make_console(), STATUS_WARN)

    def test_status_unset_contains_circle(self) -> None:
        assert "\u25cb" in _capture(_make_console(), STATUS_UNSET)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _capture_helper(func: Callable[..., None], label: str, message: object = "") -> str:
    """Call a print helper with a capturing console and return raw output."""
    import nexus.cli.theme as theme_mod

    original = theme_mod.console
    c = _make_console()
    theme_mod.console = c
    try:
        func(label, message)
    finally:
        theme_mod.console = original
    # Read what was written to the underlying StringIO buffer.
    buf = c.file
    assert isinstance(buf, io.StringIO)
    return buf.getvalue()


class TestPrintHelpers:
    """print_error / print_warning / print_success / print_info."""

    def test_print_error_with_message(self) -> None:
        out = _capture_helper(print_error, "Oops", "something broke")
        assert "Oops:" in out
        assert "something broke" in out

    def test_print_error_label_only(self) -> None:
        out = _capture_helper(print_error, "Oops")
        assert "Oops" in out
        assert ":" not in out  # no colon when message is empty

    def test_print_warning(self) -> None:
        out = _capture_helper(print_warning, "Caution", "low disk")
        assert "Caution:" in out
        assert "low disk" in out

    def test_print_success(self) -> None:
        out = _capture_helper(print_success, "Done", "sent files")
        assert "Done:" in out
        assert "files" in out

    def test_print_info(self) -> None:
        out = _capture_helper(print_info, "Note", "check docs")
        assert "Note:" in out
        assert "check docs" in out

    # -- Edge cases --

    def test_empty_message(self) -> None:
        out = _capture_helper(print_error, "Fail", "")
        assert "Fail" in out

    def test_message_with_brackets(self) -> None:
        """Literal brackets in message should not crash Rich."""
        out = _capture_helper(print_error, "Parse", "expected [int] got [str]")
        assert "Parse" in out

    def test_nested_markup_in_message(self) -> None:
        """Rich markup in the message is passed through (not double-escaped)."""
        out = _capture_helper(print_error, "Err", "[bold]nested[/bold]")
        assert "nested" in out

    def test_multiline_message(self) -> None:
        out = _capture_helper(print_error, "Err", "line1\nline2")
        assert "line1" in out
        assert "line2" in out


# ---------------------------------------------------------------------------
# NO_COLOR support
# ---------------------------------------------------------------------------


class TestNoColor:
    """Output remains readable when NO_COLOR is set."""

    def test_no_color_strips_ansi_but_keeps_text(self) -> None:
        c = _make_console(no_color=True)
        output = _capture(c, "[nexus.error]Error:[/nexus.error] broken")
        # Text content preserved.
        assert "Error:" in output
        assert "broken" in output
        # No ANSI escape codes (color sequences start with \x1b[).
        assert "\x1b[" not in output

    def test_no_color_status_icons_still_render(self) -> None:
        c = _make_console(no_color=True)
        output = _capture(c, STATUS_OK)
        assert "\u2713" in output
