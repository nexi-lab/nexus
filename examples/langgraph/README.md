# LangGraph ReAct Agent with Nexus Filesystem

This example demonstrates how to build a **ReAct (Reasoning + Acting) agent** using LangGraph that interacts with a Nexus filesystem. The agent can search, read, analyze, and write files on a remote Nexus server, making it ideal for code analysis, documentation generation, and file-based workflows.

## What is ReAct Architecture?

**ReAct** combines **Rea**soning and **Act**ing in a loop:

1. **Think** - The LLM reasons about the task and decides what to do next
2. **Act** - The agent calls tools (grep, glob, read, write) to interact with the filesystem
3. **Observe** - The agent receives results from tool execution
4. **Repeat** - The cycle continues until the task is complete

This pattern enables agents to break down complex tasks, gather information systematically, and produce useful outputs.

## Features

- **Remote Nexus Filesystem**: Connect to a shared Nexus server for persistent storage
- **Four File Operation Tools**:
  - `grep_files` - Search file content using regex patterns
  - `glob_files` - Find files by name pattern
  - `read_file` - Read file content (cat/less commands)
  - `write_file` - Write analysis results and reports
- **Multi-LLM Support**: Works with Claude (Anthropic), GPT-4 (OpenAI), or via OpenRouter
- **Educational**: Clear, commented code demonstrating agent patterns

## Quick Start

### 1. Install Dependencies

```bash
cd nexus/examples/langgraph
pip install -r requirements.txt
```

### 2. Set API Keys

Choose one LLM provider:

```bash
# Option 1: OpenRouter (recommended - access to all models with one key)
export OPENROUTER_API_KEY="sk-or-v1-..."

# Option 2: Anthropic (for Claude)
export ANTHROPIC_API_KEY="sk-ant-..."

# Option 3: OpenAI (for GPT-4)
export OPENAI_API_KEY="sk-..."
```

Optional: Set Nexus API key if your server requires authentication:

```bash
export NEXUS_API_KEY="your-nexus-key"
```

### 3. Run the Demo

```bash
python langgraph_react_demo.py
```

## Example Output

```
======================================================================
LangGraph ReAct Agent with Nexus Filesystem
======================================================================

Connecting to Nexus server at http://136.117.224.98...
✓ Connected to Nexus server

Creating Nexus file operation tools...
✓ Created 4 tools: ['grep_files', 'glob_files', 'read_file', 'write_file']

Initializing LLM...
Using Anthropic API (Claude)
✓ LLM initialized

Building ReAct agent...
✓ Agent ready

======================================================================
Available Tasks:
======================================================================
1. Search and Analyze Python Files
2. TODO Task Analysis
3. Documentation Generator

Running: Search and Analyze Python Files
======================================================================

Agent starting...

[AGENT - Reasoning]
I'll help you find and analyze Python files with async patterns. Let me start by
searching for files containing async/await.

[AGENT - Tool Calls]
  → grep_files({'grep_cmd': 'async def /workspace'})

[TOOL - grep_files]
Found 12 matches for pattern 'async def' in /workspace:

/workspace/api.py:
  Line 45: async def fetch_data(url: str):
  Line 67: async def process_batch(items: list):

/workspace/server.py:
  Line 23: async def handle_request(request):
...

[AGENT - Reasoning]
Great! I found 12 files with async patterns. Let me read a couple to understand
the patterns being used.

[AGENT - Tool Calls]
  → read_file({'read_cmd': 'less /workspace/api.py'})

[TOOL - read_file]
Preview of /workspace/api.py (first 100 lines):

import asyncio
import aiohttp

async def fetch_data(url: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            return await response.json()
...

[AGENT - Reasoning]
Now I'll write a summary report...

[AGENT - Tool Calls]
  → write_file({'path': '/reports/async-patterns.md', 'content': '# Async/Await Patterns Analysis...'})

[TOOL - write_file]
Successfully wrote 1247 bytes to /reports/async-patterns.md

[AGENT - Final Response]
Task completed! I've analyzed the Python files and created a summary report at
/reports/async-patterns.md. The codebase uses modern async/await patterns with
aiohttp for HTTP requests and asyncio for concurrency.

======================================================================
Task Complete!
======================================================================
```

## Tool Documentation

### grep_files

Search file content using grep-style commands.

```python
# Find async function definitions
grep_files("async def /workspace")

# Case-insensitive search for imports
grep_files("'import pandas' /scripts -i")

# Find TODO comments
grep_files("TODO:")
```

**Syntax**: `"pattern [path] [options]"`
- `pattern`: Required. Text or regex to search for
- `path`: Optional. Directory to search (default: `/`)
- `-i`: Optional. Case-insensitive search

### glob_files

Find files by name pattern using glob syntax.

```python
# Find all Python files
glob_files("*.py", "/workspace")

# Find all Markdown files recursively
glob_files("**/*.md", "/docs")

# Find test files
glob_files("test_*.py", "/tests")
```

**Syntax**: `glob_files(pattern, path="/")`

### read_file

Read file content using cat/less commands.

```python
# Read entire file
read_file("cat /workspace/README.md")

# Preview first 100 lines
read_file("less /scripts/large_file.py")

# Default to cat if no command specified
read_file("/data/results.json")
```

**Syntax**: `"[cat|less] path"`
- `cat`: Display entire file content
- `less`: Display first 100 lines as preview

### write_file

Write content to Nexus filesystem.

```python
# Save analysis report
write_file("/reports/summary.md", "# Summary\n\n...")

# Create configuration
write_file("/workspace/config.json", "{}")
```

**Syntax**: `write_file(path, content)`

## Architecture

### Component Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                     LangGraph ReAct Agent                    │
│                                                               │
│  ┌──────────────┐         ┌─────────────────────────────┐  │
│  │     LLM      │         │    Nexus File Tools         │  │
│  │   (Claude/   │◄────────┤  - grep_files               │  │
│  │    GPT-4)    │         │  - glob_files               │  │
│  │              │────────►│  - read_file                │  │
│  └──────────────┘         │  - write_file               │  │
│                           └─────────────┬───────────────┘  │
│                                         │                   │
└─────────────────────────────────────────┼───────────────────┘
                                          │
                                          ▼
                              ┌─────────────────────┐
                              │  Remote Nexus Server │
                              │  (http://server:8080)│
                              └─────────────────────┘
```

### ReAct Loop Flow

```
1. User provides task
       │
       ▼
2. Agent reasons about what to do
       │
       ▼
3. Agent calls tools (grep, glob, read, write)
       │
       ▼
4. Tools interact with Nexus filesystem
       │
       ▼
5. Agent observes tool results
       │
       ▼
6. Agent decides: Continue or Complete?
       │
       ├─► Continue: Go to step 2
       └─► Complete: Return final response
```

## Use Cases

### 1. Code Analysis Agent

Search through codebases to find patterns, analyze code structure, and generate documentation.

```python
# Task: Analyze async patterns
agent.invoke({
    "messages": [HumanMessage(
        "Find all async/await usage, analyze patterns, and create documentation"
    )]
})
```

### 2. Documentation Generator

Automatically generate documentation by reading code files and creating markdown summaries.

```python
# Task: Generate API docs
agent.invoke({
    "messages": [HumanMessage(
        "Find all API endpoint definitions and create API documentation"
    )]
})
```

### 3. TODO/FIXME Tracker

Scan codebase for TODO and FIXME comments and generate task lists.

```python
# Task: Create task list
agent.invoke({
    "messages": [HumanMessage(
        "Find all TODO and FIXME comments and create a prioritized task list"
    )]
})
```

### 4. Research Agent

Gather information from multiple files and synthesize insights.

```python
# Task: Research and report
agent.invoke({
    "messages": [HumanMessage(
        "Research how error handling is implemented across the codebase"
    )]
})
```

## Configuration

### Remote Nexus Server

By default, connects to `http://136.117.224.98`. Override with:

```bash
export NEXUS_SERVER_URL="http://your-server:8080"
```

### Local Testing

To test locally, start a Nexus server:

```bash
# Terminal 1: Start server
python examples/py_demo/remote_server_demo.py server

# Terminal 2: Run demo (connects to localhost:8080)
export NEXUS_SERVER_URL="http://localhost:8080"
python langgraph_react_demo.py
```

### LLM Selection

The demo tries API keys in this order:

1. **OpenRouter** (`OPENROUTER_API_KEY`) - Recommended, access to all models
2. **Anthropic** (`ANTHROPIC_API_KEY`) - For Claude
3. **OpenAI** (`OPENAI_API_KEY`) - For GPT-4

Get OpenRouter API key: https://openrouter.ai/keys

## Customization

### Adding New Tools

Add custom tools to `nexus_tools.py`:

```python
@tool
def my_custom_tool(param: str) -> str:
    """Description of what the tool does."""
    # Tool implementation
    return result
```

Then add to the tools list:

```python
def get_nexus_tools(nx):
    # ... existing tools ...

    return [grep_files, glob_files, read_file, write_file, my_custom_tool]
```

### Modifying Tasks

Edit the `tasks` list in `langgraph_react_demo.py`:

```python
tasks = [
    {
        "name": "Your Custom Task",
        "prompt": "Detailed instructions for the agent...",
    },
]
```

### Changing LLM Parameters

Modify the LLM initialization in `get_llm()`:

```python
return ChatAnthropic(
    model="claude-3-5-sonnet-20241022",
    temperature=0.7,  # Adjust creativity (0.0-1.0)
    max_tokens=4096,  # Adjust max response length
)
```

## Troubleshooting

### "No API key found"

Set one of the required API keys:

```bash
export OPENROUTER_API_KEY="sk-or-v1-..."
# or
export ANTHROPIC_API_KEY="sk-ant-..."
# or
export OPENAI_API_KEY="sk-..."
```

### "Error connecting to Nexus"

1. Check server is running: `curl http://136.117.224.98:8080/health`
2. Try local server: `python examples/py_demo/remote_server_demo.py server`
3. Set custom URL: `export NEXUS_SERVER_URL="http://localhost:8080"`

### "Module not found"

Install dependencies:

```bash
pip install -r requirements.txt
```

## Learning Resources

- **LangGraph Docs**: https://langchain-ai.github.io/langgraph/
- **ReAct Paper**: https://arxiv.org/abs/2210.03629
- **Nexus Examples**: [../README.md](../README.md)
- **Remote Nexus Setup**: [../py_demo/remote_server_demo.py](../py_demo/remote_server_demo.py)

## Next Steps

1. **Modify the example** - Try different tasks and see how the agent adapts
2. **Add more tools** - Extend with semantic search, vector similarity, etc.
3. **Multi-agent collaboration** - Have agents share results via `/shared` namespace
4. **Checkpointing** - Add state persistence using Nexus as storage
5. **Production deployment** - Scale with LangGraph Cloud or Kubernetes

## Related Examples

- [LLM Demo](../llm_demo.py) - Nexus LLM abstraction layer
- [Remote Server Demo](../py_demo/remote_server_demo.py) - Remote Nexus connection
- [Workflow Example](../workflows/workflow_example.py) - Workflow patterns

## License

Apache-2.0 (same as Nexus)
