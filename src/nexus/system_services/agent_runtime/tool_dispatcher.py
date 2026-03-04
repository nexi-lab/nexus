"""Tool dispatcher — routes LLM tool calls to Nexus kernel syscalls.

Maps tool_call names (read_file, write_file, edit_file, bash, grep, glob)
to existing NexusFS and SandboxProtocol methods. NOT a protocol — just
a router that adapts between OpenAI tool-call format and Nexus VFS.

Design doc: docs/design/AGENT-PROCESS-ARCHITECTURE.md §5.1.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.contracts.llm_types import ToolCall
    from nexus.contracts.types import OperationContext
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool schema definitions (OpenAI function-calling format)
# ---------------------------------------------------------------------------

_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "read_file": {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file at the given path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the file to read.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Line number to start reading from (1-based). Optional.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of lines to read. Optional.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    "write_file": {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file. Creates parent directories if needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the file to write.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to write to the file.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    "edit_file": {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Apply a search/replace edit to an existing file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the file to edit.",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "The exact text to find in the file.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "The text to replace it with.",
                    },
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    "grep": {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents for a text/regex pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Text or regex pattern to search for.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory to search in (default: cwd).",
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": "Glob pattern to filter files (e.g. '*.py').",
                    },
                    "ignore_case": {
                        "type": "boolean",
                        "description": "Case-insensitive search. Default: false.",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    "glob": {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files by name pattern using glob syntax.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern (e.g. '*.py', '**/*.md').",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory to search in (default: cwd).",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    "bash": {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a bash command in a sandboxed environment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The bash command to execute.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default: 300).",
                    },
                },
                "required": ["command"],
            },
        },
    },
    "python": {
        "type": "function",
        "function": {
            "name": "python",
            "description": "Execute Python code in a sandboxed environment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python code to execute.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default: 300).",
                    },
                },
                "required": ["code"],
            },
        },
    },
    "list_dir": {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and directories at the given path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path to list (default: cwd).",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "List recursively. Default: false.",
                    },
                },
                "required": [],
            },
        },
    },
}


class ToolDispatcher:
    """Routes LLM tool calls to Nexus VFS and sandbox operations.

    This is NOT a protocol — it's a concrete router that adapts between
    the OpenAI tool-call format and NexusFS/SandboxProtocol methods.
    """

    def __init__(
        self,
        vfs: NexusFS,
        sandbox: Any | None = None,
        *,
        default_cwd: str = "/",
    ) -> None:
        self._vfs = vfs
        self._sandbox = sandbox
        self._default_cwd = default_cwd

    def _resolve_path(self, path: str | None, cwd: str | None = None) -> str:
        """Resolve a path relative to cwd."""
        effective_cwd = cwd or self._default_cwd
        if path is None:
            return effective_cwd
        if path.startswith("/"):
            return path
        # Relative path — join with cwd
        if effective_cwd.endswith("/"):
            return effective_cwd + path
        return effective_cwd + "/" + path

    async def dispatch(
        self,
        ctx: OperationContext,
        tool_call: ToolCall,
        *,
        cwd: str | None = None,
        sandbox_id: str | None = None,
        sandbox_timeout: int = 300,
    ) -> str:
        """Dispatch a tool call to the appropriate Nexus subsystem.

        Args:
            ctx: Operation context for VFS permission checks.
            tool_call: The LLM tool call to dispatch.
            cwd: Current working directory for path resolution.
            sandbox_id: Sandbox ID for bash/python execution.
            sandbox_timeout: Timeout for sandbox commands.

        Returns:
            String result to feed back to the LLM.
        """
        try:
            args = json.loads(tool_call.function.arguments)
        except json.JSONDecodeError:
            return f"Error: Invalid JSON arguments: {tool_call.function.arguments}"

        name = tool_call.function.name

        try:
            match name:
                case "read_file":
                    return await self._read(ctx, args, cwd)
                case "write_file":
                    return await self._write(ctx, args, cwd)
                case "edit_file":
                    return await self._edit(ctx, args, cwd)
                case "grep":
                    return await self._grep(ctx, args, cwd)
                case "glob":
                    return await self._glob(ctx, args, cwd)
                case "list_dir":
                    return await self._list_dir(ctx, args, cwd)
                case "bash":
                    return await self._bash(args, sandbox_id, sandbox_timeout)
                case "python":
                    return await self._python(args, sandbox_id, sandbox_timeout)
                case _:
                    return f"Unknown tool: {name}"
        except Exception as exc:
            logger.warning("Tool dispatch error for %s: %s", name, exc)
            return f"Error executing {name}: {exc}"

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    async def _read(self, ctx: OperationContext, args: dict[str, Any], cwd: str | None) -> str:
        path = self._resolve_path(args.get("path"), cwd)
        content = self._vfs.sys_read(path, context=ctx)

        if isinstance(content, dict):
            content = content.get("content", b"")
        if isinstance(content, bytes):
            text = content.decode("utf-8", errors="replace")
        else:
            text = str(content)

        # Apply offset/limit if specified
        offset = args.get("offset")
        limit = args.get("limit")
        if offset is not None or limit is not None:
            lines = text.split("\n")
            start = (offset - 1) if offset and offset >= 1 else 0
            end = (start + limit) if limit else len(lines)
            text = "\n".join(lines[start:end])

        # Truncate very large files
        max_len = 50_000
        if len(text) > max_len:
            text = text[:max_len] + f"\n\n... (truncated, {len(text)} total characters)"

        return text

    async def _write(self, ctx: OperationContext, args: dict[str, Any], cwd: str | None) -> str:
        path = self._resolve_path(args.get("path"), cwd)
        content = args.get("content", "")
        content_bytes = content.encode("utf-8") if isinstance(content, str) else content
        result = self._vfs.sys_write(path, content_bytes, context=ctx)
        size = (
            result.get("size", len(content_bytes))
            if isinstance(result, dict)
            else len(content_bytes)
        )
        return f"Successfully wrote {size} bytes to {path}"

    async def _edit(self, ctx: OperationContext, args: dict[str, Any], cwd: str | None) -> str:
        path = self._resolve_path(args.get("path"), cwd)
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")

        # Read current content
        content = self._vfs.sys_read(path, context=ctx)
        if isinstance(content, dict):
            content = content.get("content", b"")
        if isinstance(content, bytes):
            text = content.decode("utf-8", errors="replace")
        else:
            text = str(content)

        if old_string not in text:
            return f"Error: old_string not found in {path}"

        # Apply edit
        new_text = text.replace(old_string, new_string, 1)
        self._vfs.sys_write(path, new_text.encode("utf-8"), context=ctx)
        return f"Successfully edited {path}"

    async def _grep(
        self,
        ctx: OperationContext,  # noqa: ARG002
        args: dict[str, Any],
        cwd: str | None,
    ) -> str:
        pattern = args.get("pattern", "")
        path = self._resolve_path(args.get("path"), cwd)
        file_pattern = args.get("file_pattern")
        ignore_case = args.get("ignore_case", False)

        try:
            results = self._vfs.grep(
                pattern=pattern,
                path=path,
                file_pattern=file_pattern,
                ignore_case=ignore_case,
                max_results=50,
            )
        except Exception:
            # Fallback: grep not available (search brick disabled)
            return "grep unavailable: search brick not enabled"

        if not results:
            return f"No matches found for '{pattern}' in {path}"

        lines = [f"Found {len(results)} matches:"]
        for m in results[:50]:
            fp = m.get("file", "?")
            ln = m.get("line", "?")
            ct = m.get("content", "").strip()[:200]
            lines.append(f"{fp}:{ln}:{ct}")
        return "\n".join(lines)

    async def _glob(
        self,
        ctx: OperationContext,  # noqa: ARG002
        args: dict[str, Any],
        cwd: str | None,
    ) -> str:
        pattern = args.get("pattern", "*")
        path = self._resolve_path(args.get("path"), cwd)

        files: list[str]
        try:
            files = self._vfs.glob(pattern, path)
        except Exception:
            # Fallback: use sys_readdir if glob not available
            try:
                raw_entries = self._vfs.sys_readdir(path, recursive=True)
                files = [str(e) for e in raw_entries]
            except Exception:
                return "glob unavailable: search brick not enabled"

        if not files:
            return f"No files found matching '{pattern}' in {path}"

        output = [f"Found {len(files)} files:"]
        for f in files[:100]:
            output.append(f"  {f}")
        if len(files) > 100:
            output.append(f"  ... and {len(files) - 100} more")
        return "\n".join(output)

    async def _list_dir(
        self,
        ctx: OperationContext,
        args: dict[str, Any],
        cwd: str | None,
    ) -> str:
        path = self._resolve_path(args.get("path"), cwd)
        recursive = args.get("recursive", False)

        try:
            entries = self._vfs.sys_readdir(path, recursive=recursive, context=ctx)
        except Exception as exc:
            return f"Error listing {path}: {exc}"

        if not entries:
            return f"Empty directory: {path}"

        output = [f"Contents of {path} ({len(entries)} entries):"]
        for e in entries[:100]:
            entry_str = e if isinstance(e, str) else str(e)
            output.append(f"  {entry_str}")
        if len(entries) > 100:
            output.append(f"  ... and {len(entries) - 100} more")
        return "\n".join(output)

    async def _bash(
        self, args: dict[str, Any], sandbox_id: str | None, default_timeout: int
    ) -> str:
        if self._sandbox is None:
            return "Error: sandbox not available"
        if sandbox_id is None:
            return "Error: no sandbox_id configured"

        command = args.get("command", "")
        timeout = args.get("timeout", default_timeout)

        result = await self._sandbox.run_code(
            sandbox_id=sandbox_id,
            language="bash",
            code=command,
            timeout=timeout,
        )

        return _format_exec_result(result)

    async def _python(
        self, args: dict[str, Any], sandbox_id: str | None, default_timeout: int
    ) -> str:
        if self._sandbox is None:
            return "Error: sandbox not available"
        if sandbox_id is None:
            return "Error: no sandbox_id configured"

        code = args.get("code", "")
        timeout = args.get("timeout", default_timeout)

        result = await self._sandbox.run_code(
            sandbox_id=sandbox_id,
            language="python",
            code=code,
            timeout=timeout,
        )

        return _format_exec_result(result)

    # ------------------------------------------------------------------
    # Schema generation
    # ------------------------------------------------------------------

    def get_tool_definitions(self, allowed_tools: tuple[str, ...]) -> list[dict[str, Any]]:
        """Return OpenAI-format tool schemas, filtered by allowed_tools."""
        return [schema for name, schema in _TOOL_SCHEMAS.items() if name in allowed_tools]


def _format_exec_result(result: Any) -> str:
    """Format sandbox execution result as a string."""
    if isinstance(result, dict):
        parts = []
        stdout = result.get("stdout", "").strip()
        stderr = result.get("stderr", "").strip()
        exit_code = result.get("exit_code", -1)

        if stdout:
            parts.append(f"Output:\n{stdout}")
        if stderr:
            parts.append(f"Errors:\n{stderr}")
        parts.append(f"Exit code: {exit_code}")
        return "\n\n".join(parts)

    return str(result)
