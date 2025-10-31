#!/usr/bin/env python3
"""Google ADK Multi-Agent System with Nexus.

This example demonstrates Google ADK's strength: coordinating multiple specialized
agents that work together on complex tasks.

Architecture:
    Coordinator Agent (orchestrates)
        ├── Researcher Agent (finds files)
        │   └── Tools: grep_files, glob_files
        ├── Analyzer Agent (reads and analyzes)
        │   └── Tools: read_file
        └── Writer Agent (creates reports)
            └── Tools: write_file

This is much harder to do in LangGraph (requires complex state management),
but natural in Google ADK!

Requirements:
    pip install google-adk nexus-ai-fs

Usage:
    export GOOGLE_API_KEY="your-key"
    python multi_agent_demo.py
"""

import os
import sys
from pathlib import Path

# Add src to path for local development
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import nexus


def connect_to_nexus():
    """Connect to Nexus filesystem."""
    server_url = os.getenv("NEXUS_URL")
    api_key = os.getenv("NEXUS_API_KEY")

    if server_url:
        from nexus.remote import RemoteNexusFS

        print(f"✓ Connected to Nexus server: {server_url}")
        return RemoteNexusFS(server_url=server_url, api_key=api_key)
    else:
        print("✓ Connected to local Nexus")
        return nexus.connect()


def create_multi_agent_system(nx):
    """
    Create a multi-agent system with specialized agents.

    This demonstrates ADK's key advantage: coordinating multiple agents.
    """
    try:
        from google.adk.agents import LlmAgent
    except ImportError:
        print("Error: google-adk not installed")
        print("Install with: pip install google-adk")
        sys.exit(1)

    # =========================================================================
    # Define shared Nexus tools
    # =========================================================================

    def grep_files(pattern: str, path: str = "/") -> str:
        """Search file content for pattern."""
        try:
            results = nx.grep(pattern, path)
            if not results:
                return f"No matches for '{pattern}'"

            lines = [f"Found {len(results)} matches:\n"]
            for match in results[:30]:
                file_path = match.get("file", "unknown")
                line_num = match.get("line", 0)
                content = match.get("content", "").strip()
                lines.append(f"{file_path}:{line_num}: {content}")

            if len(results) > 30:
                lines.append(f"\n... and {len(results) - 30} more")

            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"

    def glob_files(pattern: str, path: str = "/") -> str:
        """Find files by name pattern."""
        try:
            files = nx.glob(pattern, path)
            if not files:
                return f"No files matching '{pattern}'"

            lines = [f"Found {len(files)} files:\n"]
            lines.extend(f"  {f}" for f in files[:50])

            if len(files) > 50:
                lines.append(f"\n... and {len(files) - 50} more")

            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"

    def read_file(path: str, max_lines: int = 200) -> str:
        """Read file content."""
        try:
            content = nx.read(path)
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")

            lines = content.split("\n")
            if len(lines) > max_lines:
                preview = "\n".join(lines[:max_lines])
                return f"{path} (showing {max_lines} of {len(lines)} lines):\n\n{preview}\n\n... (truncated)"

            return f"{path}:\n\n{content}"
        except Exception as e:
            return f"Error reading {path}: {e}"

    def write_file(path: str, content: str) -> str:
        """Write content to file."""
        try:
            nx.write(path, content.encode("utf-8"))
            return f"✓ Wrote {len(content)} chars to {path}"
        except Exception as e:
            return f"Error writing {path}: {e}"

    # =========================================================================
    # Create specialized agents
    # =========================================================================

    # Agent 1: File Researcher
    researcher = LlmAgent(
        name="researcher",
        model="gemini-2.5-flash",
        description="I find and search files based on queries. I'm an expert at grep and glob.",
        instruction="""You are a file researcher. Your job is to find relevant files.

Use:
- grep_files: to search file CONTENT for patterns
- glob_files: to find files by NAME patterns

Return a clear summary of what files you found and their paths.
Focus on finding the most relevant files.""",
        tools=[grep_files, glob_files],
    )

    # Agent 2: Code Analyzer
    analyzer = LlmAgent(
        name="analyzer",
        model="gemini-2.5-flash",
        description="I read and analyze code files to extract insights, patterns, and key information.",
        instruction="""You are a code analyzer. Your job is to read files and extract insights.

Use read_file to read specific files.

When analyzing:
1. Identify key patterns and structures
2. Note important functions, classes, or configurations
3. Highlight anything unusual or noteworthy
4. Provide clear, concise summaries

Return structured insights that can be used in reports.""",
        tools=[read_file],
    )

    # Agent 3: Report Writer
    writer = LlmAgent(
        name="writer",
        model="gemini-2.5-flash",
        description="I write well-formatted reports in Markdown format based on analysis results.",
        instruction="""You are a report writer. Your job is to create clear, well-formatted reports.

Use write_file to save reports to Nexus.

Format reports in Markdown with:
- Clear headings (# ## ###)
- Bullet points for lists
- Code blocks for code examples (```python)
- Tables for comparisons
- Summary sections

Make reports professional, concise, and easy to read.""",
        tools=[write_file],
    )

    # =========================================================================
    # Create coordinator agent
    # =========================================================================

    coordinator = LlmAgent(
        name="coordinator",
        model="gemini-2.5-flash",
        description="I coordinate a team of specialists to complete complex file analysis tasks.",
        instruction="""You are a coordinator managing a team of specialists:

**TEAM:**
- researcher: Finds files using grep and glob. Ask: "Find all files matching X"
- analyzer: Reads and analyzes code. Ask: "Analyze these files: [paths]"
- writer: Creates formatted reports. Ask: "Write a report to [path] with this content: ..."

**WORKFLOW:**
1. Break down the user's task into subtasks
2. Delegate to the right specialist for each subtask
3. Coordinate their work to complete the overall goal
4. Ensure the final deliverable meets requirements

**DELEGATION:**
- Always be specific when delegating
- Pass concrete file paths or patterns to specialists
- Synthesize their results to complete the task

Work systematically through the task!""",
        sub_agents=[researcher, analyzer, writer],
    )

    return coordinator


def run_demo():
    """Run multi-agent demo."""
    print("=" * 70)
    print("Google ADK Multi-Agent System with Nexus")
    print("=" * 70)
    print()

    # Check API key
    if not os.getenv("GOOGLE_API_KEY"):
        print("Error: GOOGLE_API_KEY not set")
        print("Set with: export GOOGLE_API_KEY='your-key'")
        return

    # Connect to Nexus
    print("Connecting to Nexus...")
    nx = connect_to_nexus()

    # Create multi-agent system
    print("\nCreating multi-agent system...")
    print("  - Researcher Agent (grep, glob)")
    print("  - Analyzer Agent (read files)")
    print("  - Writer Agent (write reports)")
    print("  - Coordinator Agent (orchestrates)")
    coordinator = create_multi_agent_system(nx)
    print("✓ Multi-agent system ready")

    # Example tasks
    tasks = [
        {
            "name": "Code Analysis Report",
            "prompt": """Analyze the Python codebase and create a comprehensive report:

1. Find all Python files with async/await patterns
2. Read and analyze 3-5 representative files
3. Write a report to /reports/multi-agent-analysis.md with:
   - Executive summary
   - List of async files found
   - Common patterns and best practices observed
   - Code examples
   - Recommendations

Make it comprehensive and well-formatted!""",
        },
        {
            "name": "Security Audit",
            "prompt": """Perform a security review of the codebase:

1. Search for potential security issues (passwords, API keys, SQL queries, eval/exec)
2. Analyze any concerning files
3. Create a security report at /reports/security-audit.md
   - Summary of findings
   - Files with potential issues
   - Risk assessment
   - Recommendations

Be thorough but don't false alarm!""",
        },
        {
            "name": "Documentation Generation",
            "prompt": """Create comprehensive documentation:

1. Find all important Python modules
2. Analyze their structure and purpose
3. Generate documentation at /reports/codebase-docs.md with:
   - Overview of project structure
   - Module descriptions
   - Key classes and functions
   - Usage examples where apparent

Make it useful for new developers!""",
        },
    ]

    # Display tasks
    print("\n" + "=" * 70)
    print("Available Multi-Agent Tasks:")
    print("=" * 70)
    for i, task in enumerate(tasks, 1):
        print(f"{i}. {task['name']}")

    # Run first task
    selected = tasks[0]
    print(f"\n▶ Running: {selected['name']}")
    print("=" * 70)
    print(f"Task:\n{selected['prompt']}")
    print("=" * 70)
    print()

    print("🤖 Multi-Agent System Working...")
    print("   (Coordinator will delegate to Researcher → Analyzer → Writer)")
    print()

    try:
        # Execute with coordinator
        # The coordinator will automatically delegate to sub-agents!
        result = coordinator.execute(selected["prompt"])

        print("\n" + "=" * 70)
        print("✓ Task Complete!")
        print("=" * 70)
        print(f"\nFinal Result:\n{result}")

        # Show what was written
        if nx.exists("/reports/multi-agent-analysis.md"):
            print("\n" + "=" * 70)
            print("Generated Report Preview:")
            print("=" * 70)
            content = nx.read("/reports/multi-agent-analysis.md").decode("utf-8")
            preview_lines = content.split("\n")[:30]
            print("\n".join(preview_lines))
            if len(content.split("\n")) > 30:
                num_lines = len(content.split("\n")) - 30
                print(f"\n... ({num_lines} more lines)")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    run_demo()
