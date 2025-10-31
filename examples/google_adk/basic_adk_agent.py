#!/usr/bin/env python3
"""Google ADK Agent with Nexus Filesystem Integration.

This example demonstrates how Google ADK agents work with Nexus filesystem
operations. The agent can:

1. Search for files and patterns using grep and glob
2. Read file contents
3. Analyze and process information
4. Write results back to the filesystem

The demo automatically creates test Python files with async/await patterns,
runs the agent to analyze them, and cleans up afterwards.

Requirements:
    pip install google-adk nexus-ai-fs

Usage:
    # Set your Google API key
    export GOOGLE_API_KEY="your-key"

    # Run the demo (creates test data automatically)
    python basic_adk_agent.py

    # Keep test data after demo (optional)
    KEEP=1 python basic_adk_agent.py

Comparison to LangGraph:
    - No StateGraph setup needed (ReAct is built-in)
    - No tool binding boilerplate
    - Simpler tool definitions (just plain Python functions)
    - Same capability but cleaner code!
    - Plus: Easy to extend to multi-agent systems
"""

import os
import sys
from pathlib import Path

# Add src to path for local development
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from google.adk import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

import nexus


def connect_to_nexus(tenant_id: str = "adk-demo", agent_id: str = "file-agent"):
    """
    Connect to Nexus filesystem (local or remote).

    Args:
        tenant_id: Tenant identifier for multi-tenancy
        agent_id: Agent identifier for tracking

    Returns:
        NexusFilesystem instance
    """
    # Check if using remote server
    server_url = os.getenv("NEXUS_URL")
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
    Create Nexus tools for Google ADK.

    These are simple Python functions - no decorators needed!
    Google ADK automatically converts them to agent tools.

    Args:
        nx: NexusFilesystem instance

    Returns:
        List of functions that the agent can call
    """

    def grep_files(pattern: str, path: str, case_insensitive: bool) -> str:
        """Search file content using grep-style pattern matching.

        Use this to find files containing specific text or code patterns.

        Args:
            pattern: Text or regex pattern to search for
            path: Directory to search ("/" for entire filesystem)
            case_insensitive: Whether to ignore case

        Returns:
            Formatted string with matches including file paths and line numbers

        Examples:
            - grep_files("async def", "/workspace") → Find async functions
            - grep_files("TODO:", "/", True) → Find all TODO comments (case-insensitive)
        """
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

            return "\n".join(lines)

        except Exception as e:
            return f"Error executing grep: {str(e)}"

    def glob_files(pattern: str, path: str) -> str:
        """Find files by name pattern using glob syntax.

        Args:
            pattern: Glob pattern (e.g., "*.py", "**/*.md", "test_*.py")
            path: Directory to search

        Returns:
            List of matching file paths

        Examples:
            - glob_files("*.py", "/workspace") → All Python files
            - glob_files("**/*.md", "/docs") → All Markdown files recursively
        """
        try:
            files = nx.glob(pattern, path)

            if not files:
                return f"No files found matching '{pattern}' in {path}"

            lines = [f"Found {len(files)} files matching '{pattern}':\n"]
            lines.extend(f"  {file}" for file in files[:100])

            if len(files) > 100:
                lines.append(f"\n... and {len(files) - 100} more files")

            return "\n".join(lines)

        except Exception as e:
            return f"Error finding files: {str(e)}"

    def read_file(path: str, preview_only: bool) -> str:
        """Read file content from Nexus.

        Args:
            path: File path to read
            preview_only: If True, show only first 100 lines, else show all

        Returns:
            File content as string

        Examples:
            - read_file("/workspace/README.md") → Full content
            - read_file("/scripts/large.py", preview_only=True) → First 100 lines
        """
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
                    return f"Preview of {path} (first 100 of {len(lines)} lines):\n\n{preview}\n\n... ({len(lines) - 100} more lines)"

            return f"Content of {path}:\n\n{content}"

        except FileNotFoundError:
            return f"Error: File not found: {path}"
        except Exception as e:
            return f"Error reading file: {str(e)}"

    def write_file(path: str, content: str) -> str:
        """Write content to Nexus filesystem.

        Creates parent directories automatically. Overwrites existing files.

        Args:
            path: Absolute file path (e.g., "/reports/summary.md")
            content: Text content to write

        Returns:
            Success message with file size

        Examples:
            - write_file("/reports/analysis.md", "# Analysis\\n...") → Save report
        """
        try:
            content_bytes = content.encode("utf-8") if isinstance(content, str) else content
            nx.write(path, content_bytes)

            if nx.exists(path):
                return f"Successfully wrote {len(content_bytes)} bytes to {path}"
            else:
                return f"Error: Failed to write file {path}"

        except Exception as e:
            return f"Error writing file: {str(e)}"

    # Return all tools as a list
    return [grep_files, glob_files, read_file, write_file]


def setup_test_data(nx):
    """Set up test data in Nexus for the demo."""
    print("\n" + "=" * 70)
    print("Setting Up Test Data")
    print("=" * 70)
    print()

    # Create test Python files with async patterns
    test_files = {
        "/workspace/async_utils.py": '''"""Async utility functions for data processing."""
import asyncio
from typing import List

async def fetch_data(url: str) -> dict:
    """Fetch data from URL asynchronously."""
    await asyncio.sleep(0.1)  # Simulate network delay
    return {"url": url, "data": "example"}

async def process_batch(items: List[str]) -> List[dict]:
    """Process multiple items concurrently."""
    tasks = [fetch_data(item) for item in items]
    results = await asyncio.gather(*tasks)
    return results
''',
        "/workspace/database.py": '''"""Async database operations."""
import asyncio

class AsyncDatabase:
    """Async database connector."""

    async def connect(self):
        """Establish async database connection."""
        await asyncio.sleep(0.1)
        print("Database connected")

    async def query(self, sql: str):
        """Execute async query."""
        await asyncio.sleep(0.05)
        return [{"id": 1}]
''',
        "/workspace/web_scraper.py": '''"""Async web scraper."""
import asyncio

async def scrape_page(url: str) -> str:
    """Scrape a single page asynchronously."""
    await asyncio.sleep(0.1)
    return f"Content from {url}"

async def scrape_multiple(urls):
    """Scrape multiple pages concurrently."""
    tasks = [scrape_page(url) for url in urls]
    return await asyncio.gather(*tasks)
''',
        "/workspace/sync_utils.py": '''"""Regular synchronous utility functions."""

def process_data(data: dict) -> dict:
    """Process data synchronously."""
    return {"processed": True}

class DataProcessor:
    """Synchronous data processor."""

    def process(self, item):
        """Process single item."""
        return item
''',
    }

    print(f"Creating {len(test_files)} test Python files...")
    for path, content in test_files.items():
        nx.write(path, content.encode("utf-8"))
        print(f"✓ Created: {path}")

    print("\n✓ Test data setup complete!")
    print("  - 3 files with async/await patterns")
    print("  - 1 file with synchronous code (for comparison)")
    return test_files


def cleanup_test_data(nx, test_files):
    """Clean up test data from Nexus."""
    if os.getenv("KEEP") == "1":
        print("\n" + "=" * 70)
        print("⚠ KEEP=1 set - Test data will persist")
        print("=" * 70)
        print("\nFiles created:")
        for path in test_files:
            print(f"  - {path}")
        print("\nTo clean up manually:")
        print(
            f'  python -c "import nexus; nx = nexus.connect(); [nx.delete(p) for p in {list(test_files)}]"'
        )
        return

    print("\n" + "=" * 70)
    print("Cleaning Up Test Data")
    print("=" * 70)
    for path in test_files:
        try:
            nx.delete(path)
            print(f"✓ Deleted: {path}")
        except Exception as e:
            print(f"⚠ Could not delete {path}: {e}")

    # Clean up reports directory
    try:
        nx.delete("/reports/async-patterns.md")
        print("✓ Deleted: /reports/async-patterns.md")
    except Exception:
        pass


def run_demo():
    """Run the Google ADK demo with Nexus."""
    print("=" * 70)
    print("Google ADK Agent with Nexus Filesystem")
    print("=" * 70)
    print()

    # Check API key
    if not os.getenv("GOOGLE_API_KEY"):
        print("Error: GOOGLE_API_KEY not set")
        print("Please set your Google API key:")
        print("  export GOOGLE_API_KEY='your-key'")
        return

    # Import ADK (after checking API key) - checked at module level already
    try:
        # Verify google.adk.agents is available
        import importlib.util

        if importlib.util.find_spec("google.adk.agents") is None:
            raise ImportError("google.adk.agents not found")
    except ImportError as e:
        print(f"Error importing google.adk.agents: {e}")
        print("\nTroubleshooting:")
        print("1. Install google-adk: pip install google-adk")
        print("2. If using anaconda, ensure the package is installed in the correct environment")
        print("3. Try: import sys; sys.path.insert(0, '/path/to/site-packages')")
        print("\nNote: Google ADK may require specific Python environment setup")
        return

    # Connect to Nexus
    try:
        nx = connect_to_nexus()
    except Exception as e:
        print(f"Error connecting to Nexus: {e}")
        print("\nTo run this demo, either:")
        print("  1. Use local Nexus (default)")
        print("  2. Set NEXUS_URL to connect to remote server")
        return

    # Set up test data
    test_files = setup_test_data(nx)

    # Create tools
    print("\n" + "=" * 70)
    print("Creating Agent Tools")
    print("=" * 70)
    print("\nCreating Nexus tools for Google ADK Agent...")
    tools = create_nexus_tools(nx)
    print(f"✓ Created {len(tools)} tools: {[t.__name__ for t in tools]}")

    # Create agent
    print("\nCreating Google ADK agent...")
    from google.adk.agents import LlmAgent

    agent = LlmAgent(
        name="nexus_file_agent",
        model="gemini-2.5-flash",
        instruction="""You are a helpful filesystem assistant with access to Nexus.

You can perform the following operations:
- Search file content using grep_files (for finding text/code patterns)
- Find files by name using glob_files (for finding files by name pattern)
- Read file content using read_file
- Write files using write_file

When given a task:
1. Use grep or glob to find relevant files
2. Read files to understand their content
3. Analyze and process the information
4. Write results to the filesystem if needed

Always be thorough but concise in your analysis.""",
        description="Filesystem operations agent with Nexus integration",
        tools=tools,
    )
    print("✓ Agent created")

    # Create session service
    print("Creating session service...")
    session_service = InMemorySessionService()
    print("✓ Session service created")

    # Create runner
    print("Creating runner...")
    runner = Runner(app_name="nexus-file-agent", agent=agent, session_service=session_service)
    print("✓ Runner created")

    # Example tasks
    tasks = [
        {
            "name": "Search and Analyze Python Files",
            "prompt": (
                "Find all Python files that contain 'async def' or 'await'. "
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

    # Run the agent with Google ADK
    print("Google ADK Agent starting (processing with Runner)...\n")

    try:
        # Create user and session IDs
        user_id = "demo-user"
        session_id = "demo-session-001"

        # Create session first
        print("Creating session...")
        session_service.create_session_sync(
            app_name="nexus-file-agent", user_id=user_id, session_id=session_id
        )
        print(f"✓ Session created: {session_id}")

        # Format message as Content
        user_message = genai_types.Content(
            role="user", parts=[genai_types.Part(text=selected_task["prompt"])]
        )

        # Run agent and collect results
        print("Running agent...")
        assistant_response = []
        tool_calls = []

        for event in runner.run(user_id=user_id, session_id=session_id, new_message=user_message):
            # Process different event types
            if (
                hasattr(event, "content")
                and event.content
                and hasattr(event.content, "parts")
                and event.content.parts
            ):
                for part in event.content.parts:
                    if hasattr(part, "text") and part.text:
                        assistant_response.append(part.text)
                        print(f"\n[Assistant]: {part.text[:200]}...")
                    elif hasattr(part, "function_call") and part.function_call:
                        if hasattr(part.function_call, "name") and part.function_call.name:
                            tool_name = part.function_call.name
                            tool_calls.append(tool_name)
                            print(f"\n[Tool Call]: {tool_name}")

            if hasattr(event, "tool_response") and event.tool_response:
                print(f"[Tool Response]: {str(event.tool_response)[:100]}...")

        print("\n" + "=" * 70)
        print("Task Complete!")
        print("=" * 70)

        if assistant_response:
            print(f"\nFinal Response:\n{''.join(assistant_response)}")
        else:
            print("\nAgent completed but no text response generated.")

        if tool_calls:
            print(f"\nTools Used: {', '.join(set(tool_calls))}")

    except Exception as e:
        print(f"\nError during execution: {e}")
        import traceback

        traceback.print_exc()
    finally:
        # Clean up test data
        cleanup_test_data(nx, test_files)


if __name__ == "__main__":
    run_demo()
