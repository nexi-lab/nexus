"""Pre-loaded REPL tools for RLM sandbox access.

These functions are either:
1. Called directly from Python (for unit testing / local use)
2. Injected as source code into the sandbox REPL (for isolated execution)

The sandbox-injected versions use `requests` to call Nexus REST API
from inside the sandbox container. This reuses existing auth, ReBAC,
rate limiting, and audit logging — no custom broker needed.

Reference: Issue #1306 — API tools approach (Decision 5A, 6A)
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

# Maximum output characters shown to the model per tool call
_MAX_OUTPUT_CHARS = 8192


def nexus_read(
    path: str,
    *,
    api_url: str,
    api_key: str,
    zone_id: str,
) -> str:
    """Read a file from Nexus VFS via REST API.

    Args:
        path: Nexus VFS path (e.g., "/workspace/doc.md").
        api_url: Nexus server URL.
        api_key: API key for authentication.
        zone_id: Zone ID for scoping.

    Returns:
        File content as string, or error message on failure.
    """
    try:
        # URL-encode the path for the API call
        encoded_path = path.lstrip("/")
        resp = requests.get(
            f"{api_url}/api/v2/files/{encoded_path}",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"zone_id": zone_id},
            timeout=30,
        )
        resp.raise_for_status()
        content = resp.text
        if len(content) > _MAX_OUTPUT_CHARS:
            return content[:_MAX_OUTPUT_CHARS] + f"\n... [truncated, {len(content)} total chars]"
        return content
    except Exception as exc:
        return f"Error reading {path}: {exc}"


def nexus_search(
    query: str,
    *,
    api_url: str,
    api_key: str,
    zone_id: str,
    limit: int = 10,
    search_mode: str = "hybrid",
) -> str:
    """Search Nexus VFS via REST API.

    Args:
        query: Search query string.
        api_url: Nexus server URL.
        api_key: API key for authentication.
        zone_id: Zone ID for scoping.
        limit: Maximum number of results.
        search_mode: Search mode ("hybrid", "semantic", "bm25").

    Returns:
        Formatted search results as string, or error message on failure.
    """
    try:
        resp = requests.get(
            f"{api_url}/api/v2/files/search",
            headers={"Authorization": f"Bearer {api_key}"},
            params={
                "q": query,
                "zone_id": zone_id,
                "limit": str(limit),
                "mode": search_mode,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if not results:
            return "No results found."
        lines = []
        for i, r in enumerate(results, 1):
            path = r.get("path", "unknown")
            score = r.get("score", 0.0)
            content = r.get("content", "")[:500]
            lines.append(f"[{i}] {path} (score: {score:.2f})\n{content}")
        output = "\n\n".join(lines)
        if len(output) > _MAX_OUTPUT_CHARS:
            return output[:_MAX_OUTPUT_CHARS] + "\n... [truncated]"
        return output
    except Exception as exc:
        return f"Error searching '{query}': {exc}"


def nexus_list(
    path: str,
    *,
    api_url: str,
    api_key: str,
    zone_id: str,
) -> str:
    """List directory contents via Nexus REST API.

    Args:
        path: Nexus VFS directory path.
        api_url: Nexus server URL.
        api_key: API key for authentication.
        zone_id: Zone ID for scoping.

    Returns:
        Formatted directory listing as string, or error message on failure.
    """
    try:
        encoded_path = path.lstrip("/")
        resp = requests.get(
            f"{api_url}/api/v2/files/{encoded_path}",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"zone_id": zone_id, "list": "true"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        entries = data.get("entries", [])
        if not entries:
            return f"Empty directory: {path}"
        lines = []
        for entry in entries:
            name = entry.get("name", "?")
            etype = entry.get("type", "file")
            size = entry.get("size", "")
            size_str = f" ({size} bytes)" if size else ""
            prefix = "d" if etype == "directory" else "-"
            lines.append(f"{prefix} {name}{size_str}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Error listing {path}: {exc}"


def build_tools_injection_code(
    *,
    api_url: str,
    api_key: str,
    zone_id: str,
) -> str:
    """Generate Python code to inject Nexus tools into a sandbox REPL.

    The generated code defines nexus_read(), nexus_search(), nexus_list(),
    and FINAL() functions that are available in the sandbox's global scope.
    These functions call the Nexus REST API via HTTP using the `requests` library.

    Args:
        api_url: Nexus server URL to embed in generated code.
        api_key: API key to embed in generated code.
        zone_id: Zone ID to embed in generated code.

    Returns:
        Python source code string that can be exec'd in the sandbox.
    """
    return f'''\
import requests as _requests
import json as _json

_NEXUS_API_URL = {api_url!r}
_NEXUS_API_KEY = {api_key!r}
_NEXUS_ZONE_ID = {zone_id!r}
_MAX_OUTPUT = 8192
_HEADERS = {{"Authorization": f"Bearer {{_NEXUS_API_KEY}}"}}


def nexus_read(path: str) -> str:
    """Read a file from Nexus VFS. Returns file content as string."""
    try:
        encoded = path.lstrip("/")
        r = _requests.get(
            f"{{_NEXUS_API_URL}}/api/v2/files/{{encoded}}",
            headers=_HEADERS,
            params={{"zone_id": _NEXUS_ZONE_ID}},
            timeout=30,
        )
        r.raise_for_status()
        content = r.text
        if len(content) > _MAX_OUTPUT:
            return content[:_MAX_OUTPUT] + f"\\n... [truncated, {{len(content)}} total chars]"
        return content
    except Exception as e:
        return f"Error reading {{path}}: {{e}}"


def nexus_search(query: str, limit: int = 10, mode: str = "hybrid") -> str:
    """Search Nexus VFS. Returns formatted results."""
    try:
        r = _requests.get(
            f"{{_NEXUS_API_URL}}/api/v2/files/search",
            headers=_HEADERS,
            params={{"q": query, "zone_id": _NEXUS_ZONE_ID, "limit": limit, "mode": mode}},
            timeout=30,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            return "No results found."
        lines = []
        for i, res in enumerate(results, 1):
            p = res.get("path", "?")
            s = res.get("score", 0.0)
            c = res.get("content", "")[:500]
            lines.append(f"[{{i}}] {{p}} (score: {{s:.2f}})\\n{{c}}")
        out = "\\n\\n".join(lines)
        return out[:_MAX_OUTPUT] if len(out) > _MAX_OUTPUT else out
    except Exception as e:
        return f"Error searching '{{query}}': {{e}}"


def nexus_list(path: str = "/") -> str:
    """List directory contents in Nexus VFS."""
    try:
        encoded = path.lstrip("/")
        r = _requests.get(
            f"{{_NEXUS_API_URL}}/api/v2/files/{{encoded}}",
            headers=_HEADERS,
            params={{"zone_id": _NEXUS_ZONE_ID, "list": "true"}},
            timeout=30,
        )
        r.raise_for_status()
        entries = r.json().get("entries", [])
        if not entries:
            return f"Empty directory: {{path}}"
        lines = []
        for e in entries:
            n = e.get("name", "?")
            t = e.get("type", "file")
            sz = e.get("size", "")
            prefix = "d" if t == "directory" else "-"
            lines.append(f"{{prefix}} {{n}}" + (f" ({{sz}} bytes)" if sz else ""))
        return "\\n".join(lines)
    except Exception as e:
        return f"Error listing {{path}}: {{e}}"


def FINAL(answer: str) -> str:
    """Signal that you have reached your final answer.

    Call this function with your answer when you are done reasoning.
    Example: FINAL("The answer is 42.")
    """
    global _FINAL_ANSWER
    _FINAL_ANSWER = str(answer)
    print(f"FINAL ANSWER: {{_FINAL_ANSWER}}")
    return _FINAL_ANSWER


def FINAL_VAR(var_name: str) -> str:
    """Signal final answer using a variable from your workspace.

    Example: FINAL_VAR("result")  # Uses the value of `result` variable
    """
    val = globals().get(var_name) or locals().get(var_name)
    if val is None:
        return f"Error: variable '{{var_name}}' not found"
    return FINAL(str(val))


_FINAL_ANSWER = None
print("Nexus tools loaded: nexus_read(), nexus_search(), nexus_list(), FINAL(), FINAL_VAR()")
'''
