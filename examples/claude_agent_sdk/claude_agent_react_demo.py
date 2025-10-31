#!/usr/bin/env python3
"""Claude Agent SDK ReAct Demo with Nexus Filesystem Integration.

This example demonstrates how Claude Agent SDK provides built-in ReAct capabilities
that work seamlessly with Nexus filesystem operations. The agent can:

1. Search for files and patterns using grep and glob
2. Read file contents
3. Analyze and process information
4. Write results back to the filesystem

Unlike the LangGraph demo, Claude Agent SDK handles the ReAct loop automatically!

Requirements:
    pip install claude-sdk nexus-ai-fs

Usage:
    # Set your Anthropic API key
    export ANTHROPIC_API_KEY="your-key"

    # Optional: Set Nexus API key for remote server
    export NEXUS_API_KEY="your-nexus-key"

    # Run the demo
    python claude_agent_react_demo.py

Comparison to LangGraph:
    - No StateGraph setup needed (ReAct loop is built-in)
    - No tool binding boilerplate (just pass async functions)
    - Simpler tool definitions (no @tool decorator needed)
    - Same capability but ~70% less code!
"""

import asyncio
import os
import sys
from pathlib import Path

# Add src to path for local development
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import nexus
from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, query, tool


def connect_to_nexus(tenant_id: str = "claude-agent-demo", agent_id: str = "react-agent"):
    """
    Connect to Nexus filesystem (local or remote).

    Args:
        tenant_id: Tenant identifier for multi-tenancy
        agent_id: Agent identifier for tracking

    Returns:
        NexusFilesystem instance
    """
    # Check if using remote server
    server_url = os.getenv("NEXUS_SERVER_URL")
    api_key = os.getenv("NEXUS_API_KEY")

    if server_url:
        from nexus.remote import RemoteNexusFS

        print(f"Connecting to Nexus server at {server_url}...")
        print(f"  Tenant: {tenant_id}")
        print(f"  Agent: {agent_id}")

        nx = RemoteNexusFS(server_url=server_url, api_key=api_key)
        nx.tenant_id = tenant_id
        nx.agent_id = agent_id
        print("✓ Connected to Nexus server")
    else:
        # Use local Nexus
        print("Using local Nexus filesystem...")
        nx = nexus.connect()
        print("✓ Connected to local Nexus")

    return nx


def create_nexus_tools(nx):
    """
    Create Nexus tools for Claude Agent SDK.

    Uses @tool decorator to define tools that Claude can invoke.
    Tools are bundled into an in-process MCP server.

    Args:
        nx: NexusFilesystem instance

    Returns:
        MCP server with Nexus tools
    """

    @tool(
        "grep_files",
        "Search file content using grep-style pattern matching",
        {
            "pattern": str,
            "path": str,
            "case_insensitive": bool,
        },
    )
    async def grep_files(args):
        """Tool implementation for grep_files."""
        pattern = args.get("pattern", "")
        path = args.get("path", "/")
        case_insensitive = args.get("case_insensitive", False)

        try:
            results = nx.grep(pattern, path, ignore_case=case_insensitive)

            if not results:
                return f"No matches found for pattern '{pattern}' in {path}"

            # Format results
            lines = [f"Found {len(results)} matches for '{pattern}' in {path}:\n"]

            current_file = None
            for match in results[:50]:  # Limit to 50 matches
                file_path = match.get("file", "unknown")
                line_num = match.get("line", 0)
                content = match.get("content", "").strip()

                if file_path != current_file:
                    lines.append(f"\n{file_path}:")
                    current_file = file_path

                lines.append(f"  Line {line_num}: {content}")

            if len(results) > 50:
                lines.append(f"\n... and {len(results) - 50} more matches")

            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        except Exception as e:
            return {"content": [{"type": "text", "text": f"Error executing grep: {str(e)}"}]}

    @tool(
        "glob_files",
        "Find files by name pattern using glob syntax",
        {
            "pattern": str,
            "path": str,
        },
    )
    async def glob_files(args):
        """Tool implementation for glob_files."""
        pattern = args.get("pattern", "")
        path = args.get("path", "/")

        try:
            files = nx.glob(pattern, path)

            if not files:
                return f"No files found matching '{pattern}' in {path}"

            lines = [f"Found {len(files)} files matching '{pattern}':\n"]
            lines.extend(f"  {file}" for file in files[:100])

            if len(files) > 100:
                lines.append(f"\n... and {len(files) - 100} more files")

            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        except Exception as e:
            return {"content": [{"type": "text", "text": f"Error finding files: {str(e)}"}]}

    @tool(
        "read_file",
        "Read file content from Nexus filesystem",
        {
            "path": str,
            "preview_only": bool,
        },
    )
    async def read_file(args):
        """Tool implementation for read_file."""
        path = args.get("path", "")
        preview_only = args.get("preview_only", False)

        try:
            content = nx.read(path)

            # Handle bytes
            if isinstance(content, bytes):
                content = content.decode("utf-8")

            # Preview mode
            if preview_only:
                lines = content.split("\n")
                if len(lines) > 100:
                    preview = "\n".join(lines[:100])
                    text = f"Preview of {path} (first 100 of {len(lines)} lines):\n\n{preview}\n\n... ({len(lines) - 100} more lines)"
                else:
                    text = f"Content of {path}:\n\n{content}"
            else:
                text = f"Content of {path}:\n\n{content}"

            return {"content": [{"type": "text", "text": text}]}

        except FileNotFoundError:
            return {"content": [{"type": "text", "text": f"Error: File not found: {path}"}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Error reading file: {str(e)}"}]}

    @tool(
        "write_file",
        "Write content to Nexus filesystem",
        {
            "path": str,
            "content": str,
        },
    )
    async def write_file(args):
        """Tool implementation for write_file."""
        path = args.get("path", "")
        content = args.get("content", "")

        try:
            content_bytes = content.encode("utf-8") if isinstance(content, str) else content
            nx.write(path, content_bytes)

            if nx.exists(path):
                text = f"Successfully wrote {len(content_bytes)} bytes to {path}"
            else:
                text = f"Error: Failed to write file {path}"

            return {"content": [{"type": "text", "text": text}]}

        except Exception as e:
            return {"content": [{"type": "text", "text": f"Error writing file: {str(e)}"}]}

    # Bundle tools into an in-process MCP server
    server = create_sdk_mcp_server(
        name="nexus-tools", version="1.0.0", tools=[grep_files, glob_files, read_file, write_file]
    )

    return server


async def run_demo():
    """Run the Claude Agent SDK ReAct demo."""
    print("=" * 70)
    print("Claude Agent SDK ReAct Demo with Nexus Filesystem")
    print("=" * 70)
    print()

    # Check API key
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY not set")
        print("Please set your Anthropic API key:")
        print("  export ANTHROPIC_API_KEY='your-key'")
        return

    # Connect to Nexus
    try:
        nx = connect_to_nexus()
    except Exception as e:
        print(f"Error connecting to Nexus: {e}")
        print("\nTo run this demo, either:")
        print("  1. Use local Nexus (default)")
        print("  2. Set NEXUS_SERVER_URL to connect to remote server")
        return

    # Create tools
    print("\nCreating Nexus tools for Claude Agent SDK...")
    mcp_server = create_nexus_tools(nx)
    print("✓ Created Nexus tools MCP server")

    # Example tasks
    tasks = [
        {
            "name": "Search and Analyze Python Files",
            "prompt": (
                "Find all Python files in /workspace that contain 'async def' or 'await'. "
                "Read a couple of them to understand the async patterns being used. "
                "Then write a summary report to /reports/async-patterns.md that includes:\n"
                "1. Number of files using async/await\n"
                "2. Common async patterns you observed\n"
                "3. List of files reviewed\n\n"
                "Keep the report concise but informative."
            ),
        },
        {
            "name": "TODO Task Analysis",
            "prompt": (
                "Search for all TODO and FIXME comments in the codebase. "
                "Categorize them by priority or type if possible. "
                "Write a task list to /reports/todo-list.md."
            ),
        },
        {
            "name": "Documentation Generator",
            "prompt": (
                "Find all Python files in /workspace. "
                "Generate a brief documentation overview in /reports/code-structure.md "
                "that lists the main modules and their apparent purposes."
            ),
        },
    ]

    # Display available tasks
    print("\n" + "=" * 70)
    print("Available Tasks:")
    print("=" * 70)
    for i, task in enumerate(tasks, 1):
        print(f"{i}. {task['name']}")

    # Run first task by default
    selected_task = tasks[0]

    print(f"\nRunning: {selected_task['name']}")
    print("=" * 70)
    print(f"Task: {selected_task['prompt']}")
    print("=" * 70)
    print()

    # Run the agent with Claude Agent SDK
    # The ReAct loop is built-in - just call query()!
    print("Claude Agent starting (ReAct loop is automatic)...\n")

    try:
        # Configure options with MCP server
        options = ClaudeAgentOptions(
            mcp_servers={"nexus": mcp_server},
            allowed_tools=[
                "mcp__nexus__grep_files",
                "mcp__nexus__glob_files",
                "mcp__nexus__read_file",
                "mcp__nexus__write_file",
            ],
        )

        # Stream responses from Claude (all args must be keyword-only!)
        try:
            async for message in query(prompt=selected_task["prompt"], options=options):
                # Pretty print messages based on type
                msg_type = type(message).__name__

                if msg_type == "AssistantMessage":
                    # Claude's responses
                    if hasattr(message, "content") and message.content:
                        for block in message.content:
                            block_type = type(block).__name__

                            if block_type == "TextBlock":
                                # Claude's thinking/reasoning
                                print("\n🤖 Claude:")
                                print(f"   {block.text}")

                            elif block_type == "ToolUseBlock":
                                # Claude calling a tool
                                print(f"\n🔧 Calling tool: {block.name}")
                                # Pretty print the input
                                for key, value in block.input.items():
                                    if isinstance(value, str) and len(value) > 60:
                                        print(f"   • {key}: {value[:60]}...")
                                    else:
                                        print(f"   • {key}: {value}")

                elif msg_type == "UserMessage":
                    # Tool results coming back
                    if hasattr(message, "content") and message.content:
                        for block in message.content:
                            block_type = type(block).__name__

                            if block_type == "ToolResultBlock":
                                if hasattr(block, "is_error") and block.is_error:
                                    print(f"   ❌ Error: {block.content}")
                                else:
                                    print("   ✓ Result:")
                                    # Format the result
                                    content = str(block.content)
                                    if len(content) > 300:
                                        # Show first 300 chars
                                        lines = content[:300].split("\n")
                                        for line in lines[:10]:  # Max 10 lines
                                            print(f"      {line}")
                                        print(f"      ... ({len(content)} characters total)")
                                    else:
                                        for line in content.split("\n")[:15]:  # Max 15 lines
                                            print(f"      {line}")

                elif msg_type == "SystemMessage":
                    # System messages (rarely shown)
                    if hasattr(message, "content"):
                        print(f"\n⚙️  System: {message.content}")
                    else:
                        # SystemMessage might have 'text' instead
                        content = getattr(message, "text", str(message))
                        print(f"\n⚙️  System: {content}")

                # Uncomment for debugging:
                # else:
                #     print(f"\n[DEBUG {msg_type}]: {message}")

        except (RuntimeError, GeneratorExit) as e:
            # SDK cleanup errors - safe to ignore, task likely completed
            if "cancel scope" in str(e) or "GeneratorExit" in str(type(e).__name__):
                pass  # Normal cleanup, ignore
            else:
                raise

        print("\n" + "=" * 70)
        print("✅ Task Complete!")
        print("=" * 70)

        # Check if any files were created
        try:
            # Look for the expected output file
            if nx.exists("/reports/async-patterns.md"):
                print("\n📄 Report generated: /reports/async-patterns.md")

                # Show a preview
                content = nx.read("/reports/async-patterns.md")
                if isinstance(content, bytes):
                    content = content.decode("utf-8")

                lines = content.split("\n")
                preview_lines = min(10, len(lines))
                print(f"\n   Preview (first {preview_lines} lines):")
                for line in lines[:preview_lines]:
                    print(f"   {line}")

                if len(lines) > preview_lines:
                    print(f"   ... ({len(lines) - preview_lines} more lines)")

                print(f"\n   Total: {len(lines)} lines, {len(content)} characters")
        except Exception:
            pass  # Output file might not exist yet

    except Exception as e:
        print(f"\n❌ Error during execution: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(run_demo())
