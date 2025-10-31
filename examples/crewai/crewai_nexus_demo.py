#!/usr/bin/env python3
"""CrewAI + Nexus MCP Integration Demo.

This example demonstrates how to integrate CrewAI agents with Nexus filesystem
using the Model Context Protocol (MCP). The agents can:

1. Search, read, and write files on Nexus filesystem
2. Store and retrieve memories for long-term learning
3. Perform semantic search across documents
4. Execute workflows and collaborate via shared storage

The demo uses remote mode where the Nexus server runs separately and agents
connect via MCP stdio transport.

Requirements:
    pip install -r requirements.txt

Usage:
    # Terminal 1: Start Nexus server
    ./start_nexus_server.sh

    # Terminal 2: Run the demo
    export ANTHROPIC_API_KEY="your-key"  # or OPENAI_API_KEY
    python crewai_nexus_demo.py

Features:
    - MCP integration for tool discovery
    - Multi-agent collaboration via Nexus
    - Memory persistence across sessions
    - Semantic search and file operations
    - Workflow automation
"""

import contextlib
import os
import sys
from pathlib import Path

# Add src to path for local development
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from crewai import Agent, Crew, Task
from crewai.tools import tool

# =========================================================================
# NEXUS MCP TOOLS (Alternative to crewai_tools.MCPServerAdapter)
# =========================================================================
# Since we want fine control and MCPServerAdapter may not be stable yet,
# we'll create custom tools that call the MCP server via subprocess


def call_nexus_mcp(tool_name: str, **kwargs) -> str:
    """Call a Nexus MCP tool via stdio transport."""
    import json

    nexus_url = os.getenv("NEXUS_URL", "http://localhost:8080")
    nexus_api_key = os.getenv("NEXUS_API_KEY", "")

    # Call via nexus CLI
    env = os.environ.copy()
    if nexus_url:
        env["NEXUS_URL"] = nexus_url
    if nexus_api_key:
        env["NEXUS_API_KEY"] = nexus_api_key

    try:
        # Use nexus Python API directly for reliability
        from nexus import connect

        nx = connect(config={"remote_url": nexus_url, "api_key": nexus_api_key})

        # Map tool names to Nexus methods
        if tool_name == "nexus_read_file":
            content = nx.read(kwargs["path"])
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
            return content
        elif tool_name == "nexus_write_file":
            content = kwargs["content"].encode("utf-8")
            nx.write(kwargs["path"], content)
            return f"Successfully wrote to {kwargs['path']}"
        elif tool_name == "nexus_list_files":
            files = nx.list(kwargs.get("path", "/"), recursive=kwargs.get("recursive", False))
            return "\n".join(files) if files else "No files found"
        elif tool_name == "nexus_grep":
            results = nx.grep(kwargs["pattern"], kwargs.get("path", "/"))
            if not results:
                return "No matches found"
            output = []
            for result in results[:20]:  # Limit to 20 results
                output.append(
                    f"{result.get('path', 'unknown')}:{result.get('line', 0)}: {result.get('content', '')}"
                )
            return "\n".join(output)
        elif tool_name == "nexus_glob":
            matches = nx.glob(kwargs["pattern"], kwargs.get("path", "/"))
            return "\n".join(matches) if matches else "No matches found"
        elif tool_name == "nexus_semantic_search":
            results = nx.search(kwargs["query"], limit=kwargs.get("limit", 10))
            if not results:
                return "No results found"
            import json

            return json.dumps(results, indent=2)
        elif tool_name == "nexus_store_memory":
            nx.memory.store(
                kwargs["content"],
                scope="user",
                memory_type=kwargs.get("memory_type"),
                importance=kwargs.get("importance", 0.5),
            )
            nx.memory.session.commit()
            return f"Stored memory: {kwargs['content'][:80]}..."
        elif tool_name == "nexus_query_memory":
            memories = nx.memory.search(kwargs["query"], scope="user", limit=kwargs.get("limit", 5))
            import json

            return json.dumps(memories, indent=2)
        else:
            return f"Unknown tool: {tool_name}"
    except Exception as e:
        return f"Error calling {tool_name}: {str(e)}"


# =========================================================================
# CREWAI TOOLS - Wrapped Nexus MCP Tools
# =========================================================================


@tool("Read File")
def read_file(path: str) -> str:
    """Read a file from Nexus filesystem.

    Args:
        path: File path to read (e.g., '/workspace/data.txt')

    Returns:
        File content as string
    """
    return call_nexus_mcp("nexus_read_file", path=path)


@tool("Write File")
def write_file(path: str, content: str) -> str:
    """Write content to a file in Nexus filesystem.

    Args:
        path: File path to write (e.g., '/workspace/report.md')
        content: Content to write

    Returns:
        Success message
    """
    return call_nexus_mcp("nexus_write_file", path=path, content=content)


@tool("List Files")
def list_files(path: str = "/", recursive: bool = False) -> str:
    """List files in a directory.

    Args:
        path: Directory path (default: '/')
        recursive: List recursively (default: False)

    Returns:
        List of file paths
    """
    return call_nexus_mcp("nexus_list_files", path=path, recursive=recursive)


@tool("Search Files by Pattern")
def glob_files(pattern: str, path: str = "/") -> str:
    """Find files matching a glob pattern.

    Args:
        pattern: Glob pattern (e.g., '*.py', '**/*.md')
        path: Base path to search from

    Returns:
        List of matching file paths
    """
    return call_nexus_mcp("nexus_glob", pattern=pattern, path=path)


@tool("Search File Contents")
def grep_files(pattern: str, path: str = "/") -> str:
    """Search file contents using regex pattern.

    Args:
        pattern: Regex pattern to search for
        path: Base path to search from

    Returns:
        Matching lines with file paths and line numbers
    """
    return call_nexus_mcp("nexus_grep", pattern=pattern, path=path)


@tool("Semantic Search")
def semantic_search(query: str, limit: int = 10) -> str:
    """Search files semantically using natural language.

    Args:
        query: Natural language search query
        limit: Maximum number of results (default: 10)

    Returns:
        Search results with relevance scores
    """
    return call_nexus_mcp("nexus_semantic_search", query=query, limit=limit)


@tool("Store Memory")
def store_memory(content: str, memory_type: str = None, importance: float = 0.5) -> str:
    """Store a memory in Nexus for long-term learning.

    Args:
        content: Memory content to store
        memory_type: Optional category (e.g., 'insight', 'fact')
        importance: Importance score 0.0-1.0 (default: 0.5)

    Returns:
        Success message
    """
    return call_nexus_mcp(
        "nexus_store_memory", content=content, memory_type=memory_type, importance=importance
    )


@tool("Query Memory")
def query_memory(query: str, limit: int = 5) -> str:
    """Retrieve relevant memories using semantic search.

    Args:
        query: Search query
        limit: Maximum number of results (default: 5)

    Returns:
        Matching memories with metadata
    """
    return call_nexus_mcp("nexus_query_memory", query=query, limit=limit)


# =========================================================================
# DEMO TASKS
# =========================================================================


def demo_1_file_analysis():
    """Demo 1: File Analysis - Search, read, and analyze files."""

    print("\n" + "=" * 70)
    print("Demo 1: File Analysis Agent")
    print("=" * 70)

    # Create agent with file operation tools
    analyst = Agent(
        role="Code Analyst",
        goal="Analyze Python files and identify patterns",
        backstory="""You are an expert code analyst who excels at finding patterns,
        identifying issues, and providing insights about codebases. You systematically
        search for files, read their contents, and create comprehensive reports.""",
        tools=[glob_files, read_file, grep_files, write_file],
        verbose=True,
    )

    # Create task
    task = Task(
        description="""Analyze Python files in /workspace:
        1. Find all Python files using glob
        2. Search for 'async def' patterns using grep
        3. Read a few example files to understand the patterns
        4. Create a summary report at /reports/async-analysis.md

        Focus on understanding how async/await is used in the codebase.""",
        expected_output="A markdown report summarizing async patterns found in Python files",
        agent=analyst,
    )

    # Execute
    crew = Crew(agents=[analyst], tasks=[task], verbose=True)
    result = crew.kickoff()

    print("\n" + "=" * 70)
    print("Result:")
    print("=" * 70)
    print(result)

    return result


def demo_2_research_with_memory():
    """Demo 2: Research Agent with Memory - Learn and remember insights."""

    print("\n" + "=" * 70)
    print("Demo 2: Research Agent with Memory")
    print("=" * 70)

    # Create researcher with memory tools
    researcher = Agent(
        role="Research Analyst",
        goal="Research topics and build knowledge over time",
        backstory="""You are a diligent researcher who not only finds information
        but also remembers key insights for future reference. You use semantic search
        to find relevant documents and store important learnings in memory.""",
        tools=[semantic_search, read_file, store_memory, query_memory],
        verbose=True,
    )

    # Task 1: Research and store insights
    research_task = Task(
        description="""Research error handling patterns:
        1. Use semantic search to find files about error handling
        2. Read the most relevant files
        3. Store key insights in memory with high importance
        4. Tag memories with type 'error_handling_pattern'

        Focus on best practices and common patterns.""",
        expected_output="Summary of error handling patterns with key insights stored in memory",
        agent=researcher,
    )

    # Task 2: Recall and synthesize
    synthesis_task = Task(
        description="""Synthesize previous learnings:
        1. Query memory for 'error_handling_pattern' insights
        2. Combine with any new findings
        3. Create a comprehensive guide

        Build upon what you learned in the previous task.""",
        expected_output="Comprehensive guide based on stored memories",
        agent=researcher,
    )

    # Execute
    crew = Crew(agents=[researcher], tasks=[research_task, synthesis_task], verbose=True)
    result = crew.kickoff()

    print("\n" + "=" * 70)
    print("Result:")
    print("=" * 70)
    print(result)

    return result


def demo_3_multi_agent_collaboration():
    """Demo 3: Multi-Agent Collaboration - Agents working together via Nexus."""

    print("\n" + "=" * 70)
    print("Demo 3: Multi-Agent Collaboration")
    print("=" * 70)

    # Agent 1: Data Collector
    collector = Agent(
        role="Data Collector",
        goal="Collect and organize information from files",
        backstory="""You are a meticulous data collector who finds and organizes
        information systematically. You prepare data for analysis by other agents.""",
        tools=[glob_files, grep_files, list_files, write_file],
        verbose=True,
    )

    # Agent 2: Analyst
    analyst = Agent(
        role="Senior Analyst",
        goal="Analyze collected data and generate insights",
        backstory="""You are an experienced analyst who reads collected data,
        identifies patterns, and generates actionable insights.""",
        tools=[read_file, semantic_search, write_file, store_memory],
        verbose=True,
    )

    # Task 1: Collect data
    collect_task = Task(
        description="""Collect TODO comments from the codebase:
        1. Search for 'TODO' patterns in all files
        2. Organize findings by file
        3. Write collected data to /workspace/todo-collection.txt

        Make it easy for the analyst to review.""",
        expected_output="Organized list of TODO items saved to file",
        agent=collector,
    )

    # Task 2: Analyze (depends on collect_task)
    analyze_task = Task(
        description="""Analyze the collected TODO items:
        1. Read /workspace/todo-collection.txt
        2. Categorize TODOs by priority and type
        3. Generate recommendations
        4. Write report to /reports/todo-analysis.md
        5. Store key insights in memory

        Provide actionable recommendations.""",
        expected_output="Analysis report with categorized TODOs and recommendations",
        agent=analyst,
    )

    # Execute sequential workflow
    crew = Crew(
        agents=[collector, analyst],
        tasks=[collect_task, analyze_task],
        verbose=True,
    )
    result = crew.kickoff()

    print("\n" + "=" * 70)
    print("Result:")
    print("=" * 70)
    print(result)

    return result


# =========================================================================
# MAIN
# =========================================================================


def check_environment():
    """Check that required environment variables are set."""

    print("Checking environment...")

    # Check LLM API key
    has_api_key = False
    if os.getenv("ANTHROPIC_API_KEY"):
        print("✓ Using Anthropic API (Claude)")
        has_api_key = True
    elif os.getenv("OPENAI_API_KEY"):
        print("✓ Using OpenAI API (GPT-4)")
        has_api_key = True
    elif os.getenv("OPENROUTER_API_KEY"):
        print("✓ Using OpenRouter API")
        has_api_key = True

    if not has_api_key:
        print("\n✗ Error: No LLM API key found!")
        print("\nSet one of:")
        print("  export ANTHROPIC_API_KEY='your-key'")
        print("  export OPENAI_API_KEY='your-key'")
        print("  export OPENROUTER_API_KEY='your-key'")
        sys.exit(1)

    # Check Nexus connection
    nexus_url = os.getenv("NEXUS_URL", "http://localhost:8080")
    print(f"\nNexus server: {nexus_url}")

    # Test connection
    try:
        from nexus import connect

        nx = connect(config={"remote_url": nexus_url})
        nx.list("/")
        print("✓ Connected to Nexus server")
        nx.close()
    except Exception as e:
        print(f"\n✗ Error connecting to Nexus: {e}")
        print("\nMake sure Nexus server is running:")
        print("  Terminal 1: ./start_nexus_server.sh")
        print("  Terminal 2: python crewai_nexus_demo.py")
        sys.exit(1)

    print("\n✓ Environment check passed!\n")


def setup_test_data():
    """Setup test data for demos."""

    print("Setting up test data...")

    try:
        from nexus import connect

        nx = connect(config={"remote_url": os.getenv("NEXUS_URL", "http://localhost:8080")})

        # Create directories
        for dir_path in ["/workspace", "/reports"]:
            with contextlib.suppress(Exception):
                nx.mkdir(dir_path)  # Directory may already exist

        # Create sample Python files with async patterns
        sample_files = {
            "/workspace/api_client.py": """import asyncio
import aiohttp

async def fetch_data(url: str) -> dict:
    '''Fetch data from API endpoint.'''
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            return await response.json()

async def fetch_multiple(urls: list[str]) -> list[dict]:
    '''Fetch data from multiple URLs concurrently.'''
    # TODO: Add error handling for failed requests
    tasks = [fetch_data(url) for url in urls]
    return await asyncio.gather(*tasks)
""",
            "/workspace/database.py": """import asyncpg

async def get_connection():
    '''Get database connection.'''
    return await asyncpg.connect('postgresql://localhost/mydb')

async def query_users(min_age: int) -> list:
    '''Query users from database.'''
    conn = await get_connection()
    # TODO: Implement connection pooling
    try:
        return await conn.fetch('SELECT * FROM users WHERE age >= $1', min_age)
    finally:
        await conn.close()
""",
            "/workspace/utils.py": """def process_data(data: dict) -> dict:
    '''Synchronous data processing.'''
    # TODO: Consider making this async for large datasets
    return {k: v.upper() if isinstance(v, str) else v for k, v in data.items()}

def validate_input(data: dict) -> bool:
    '''Validate input data.'''
    required_fields = ['id', 'name', 'email']
    return all(field in data for field in required_fields)
""",
        }

        for path, content in sample_files.items():
            nx.write(path, content.encode("utf-8"))

        print(f"✓ Created {len(sample_files)} test files in /workspace")
        nx.close()

    except Exception as e:
        print(f"Warning: Could not setup test data: {e}")
        print("Continuing anyway...")


def main():
    """Main entry point."""

    print("=" * 70)
    print("CrewAI + Nexus MCP Integration Demo")
    print("=" * 70)

    # Check environment
    check_environment()

    # Setup test data
    setup_test_data()

    # Run demos
    demos = [
        ("File Analysis", demo_1_file_analysis),
        ("Research with Memory", demo_2_research_with_memory),
        ("Multi-Agent Collaboration", demo_3_multi_agent_collaboration),
    ]

    print("\nAvailable demos:")
    for i, (name, _) in enumerate(demos, 1):
        print(f"  {i}. {name}")
    print(f"  {len(demos) + 1}. Run all demos")

    choice = input(f"\nSelect demo (1-{len(demos) + 1}, default=1): ").strip()

    if not choice:
        choice = "1"

    try:
        choice_num = int(choice)
        if choice_num == len(demos) + 1:
            # Run all demos
            for name, demo_func in demos:
                print(f"\n\n{'=' * 70}")
                print(f"Running: {name}")
                print(f"{'=' * 70}")
                demo_func()
        elif 1 <= choice_num <= len(demos):
            # Run selected demo
            name, demo_func = demos[choice_num - 1]
            demo_func()
        else:
            print(f"Invalid choice: {choice_num}")
            sys.exit(1)
    except ValueError:
        print(f"Invalid input: {choice}")
        sys.exit(1)

    print("\n" + "=" * 70)
    print("Demo completed!")
    print("=" * 70)


if __name__ == "__main__":
    main()
