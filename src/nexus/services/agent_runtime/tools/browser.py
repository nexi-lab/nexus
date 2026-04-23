"""BrowserTool — browser automation via ai_dev_browser.

Dispatches to ai_dev_browser.core.<command> directly (no subprocess).
Tab-based commands auto-connect to the running browser via get_active_tab().
Lifecycle commands (browser_start, browser_list, etc.) run without a tab.

Commands: page_goto, click_by_text, type_by_text, page_discover,
          page_screenshot, page_html, browser_start, browser_list,
          tab_new, tab_list, tab_switch, tab_close, and more.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

_RESULT_LIMIT = 100_000
# Commands that don't require a browser tab (lifecycle / browser-level ops)
_NO_TAB_COMMANDS = frozenset(
    [
        "browser_start",
        "browser_stop",
        "browser_list",
        "browser_connect",
    ]
)


class BrowserTool:
    """Browser automation. Navigate pages, click elements, type text,
    take screenshots, discover page structure."""

    name = "browser"
    description = (
        "Browser automation tool. Navigate pages, click elements, type text, "
        "take screenshots, discover page structure.\n"
        "Common commands: page_goto, click_by_text, type_by_text, page_discover, "
        "page_screenshot, page_html, find_by_text, browser_start, tab_new."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "Browser command name, e.g. page_goto, click_by_text, "
                    "type_by_text, page_discover, page_screenshot, page_html, "
                    "find_by_text, browser_start, browser_list, tab_new, "
                    "tab_list, tab_switch, tab_close."
                ),
            },
            "args": {
                "type": "object",
                "description": "Command arguments as key-value pairs.",
            },
        },
        "required": ["command"],
    }

    async def call(self, *, command: str, args: dict | None = None, **_: Any) -> str:
        args = args or {}
        try:
            result = await asyncio.to_thread(_run_browser_command, command, args)
        except Exception as exc:
            return json.dumps({"error": str(exc), "command": command})

        if isinstance(result, str):
            return result[:_RESULT_LIMIT]
        text = json.dumps(result)
        if len(text) > _RESULT_LIMIT:
            return text[:_RESULT_LIMIT] + f"\n... (truncated, {len(text)} total chars)"
        return text

    def is_read_only(self) -> bool:
        return False

    def is_concurrent_safe(self) -> bool:
        return False


def _run_browser_command(command: str, args: dict) -> Any:
    """Blocking helper run inside asyncio.to_thread."""
    import importlib

    # Resolve the core function
    try:
        core_mod = importlib.import_module("ai_dev_browser.core")
    except ImportError as exc:
        raise RuntimeError(
            f"ai_dev_browser not available: {exc}. Ensure ai_dev_browser is on PYTHONPATH."
        ) from exc

    func = getattr(core_mod, command, None)
    if func is None:
        raise ValueError(
            f"Unknown browser command: {command!r}. "
            "Use page_discover or check ai_dev_browser docs for available commands."
        )

    if not callable(func):
        raise ValueError(f"browser.{command} is not callable")

    # Detect whether the function needs a tab as first positional argument
    params = list(inspect.signature(func).parameters.keys())
    needs_tab = params and params[0] in ("tab", "browser_or_tab")

    # Run in a fresh event loop (we're already in a thread)
    loop = asyncio.new_event_loop()
    try:
        if needs_tab and command not in _NO_TAB_COMMANDS:
            return loop.run_until_complete(_call_with_tab(func, args))
        else:
            if inspect.iscoroutinefunction(func):
                return loop.run_until_complete(func(**args))
            else:
                return func(**args)
    finally:
        loop.close()


async def _call_with_tab(func: Any, args: dict) -> Any:
    from ai_dev_browser.core import get_active_tab

    tab = await get_active_tab()
    if inspect.iscoroutinefunction(func):
        return await func(tab, **args)
    else:
        return func(tab, **args)
