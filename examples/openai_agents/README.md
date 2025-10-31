# OpenAI Agents SDK with Nexus Filesystem

This example demonstrates how to build **ReAct (Reasoning + Acting) agents** using the OpenAI Agents SDK that interact with a Nexus filesystem. The agent can search, read, analyze, and write files, with persistent memory across sessions.

## What is the OpenAI Agents SDK?

The **OpenAI Agents SDK** is a production-ready framework for building agentic AI applications. It's an upgrade to OpenAI's Swarm framework with:

- **Built-in ReAct Loop** - Automatic reasoning and tool calling
- **Function Tools** - Turn any Python function into a tool with `@function_tool`
- **Handoffs** - Native agent-to-agent delegation
- **Guardrails** - Input/output validation
- **Sessions** - Conversation history management

## Why Nexus + OpenAI Agents SDK?

| Feature | Benefit |
|---------|---------|
| **Persistent File System** | Read/write files that persist across runs |
| **Memory API** | Remember facts, preferences, experiences across sessions |
| **Semantic Search** | Find relevant files by meaning, not just keywords |
| **Multi-Tenancy** | Isolated workspaces per user/team |
| **Workflows** | Trigger agents on file events |
| **Minimal Code** | 70% less boilerplate vs manual ReAct loops |
| **Automatic ReAct** | Built-in Think→Act→Observe loop |

## Quick Start

### 1. Install Dependencies

```bash
cd nexus/examples/openai_agents
pip install -r requirements.txt
```

### 2. Set API Key

```bash
export OPENAI_API_KEY="your-openai-api-key"
```

### 3. Run Examples

**Option A: All-in-one script (Recommended)**

```bash
# Starts server, creates sample files, runs agent
./openai_agent_react_demo.sh
```

**Option B: Manual execution**

```bash
# Start Nexus server first
cd ../..
./scripts/init-nexus-with-auth.sh

# In another terminal:
source .nexus-admin-env
python examples/openai_agents/openai_agent_react_demo.py

# Or memory demo
python examples/openai_agents/memory_agent_demo.py
```

## Examples Overview

### 1. File Operations Demo (`openai_agent_react_demo.py`)

Port of the LangGraph ReAct demo showing file search, read, and analysis capabilities.

**Features:**
- Search files with `grep_files` (pattern matching)
- Find files with `glob_files` (name patterns)
- Read files with `read_file` (full or preview mode)
- Write reports with `write_file`

**Example Task:**
```python
"Find all Python files with async/await patterns and write a summary to /reports/async-analysis.md"
```

### 2. Memory Agent Demo (`memory_agent_demo.py`)

Shows how to use Nexus Memory API for persistent agent memory.

**Features:**
- `store_memory` - Save facts, preferences, experiences
- `recall_memory` - Semantic search over memories
- `list_all_memories` - View all stored memories
- Memory persists across Python sessions

**Example Conversation:**
```
User: "Remember that I prefer Python over JavaScript"
Agent: ✓ Stored preference: I prefer Python over JavaScript

User: "What programming language do I prefer?"
Agent: Based on my memory, you prefer Python over JavaScript.
```

## Architecture

### Component Diagram

```
┌─────────────────────────────────────────────────────────────┐
│              OpenAI Agents SDK ReAct Agent                   │
│                                                               │
│  ┌──────────────┐         ┌─────────────────────────────┐  │
│  │  GPT-4/4o    │         │    Nexus Tools              │  │
│  │   (OpenAI)   │◄────────┤  @function_tool decorated   │  │
│  │              │         │  - grep_files               │  │
│  │   Built-in   │────────►│  - glob_files               │  │
│  │  ReAct Loop  │         │  - read_file                │  │
│  │              │         │  - write_file               │  │
│  └──────────────┘         │  - store_memory (optional)  │  │
│                           │  - recall_memory (optional) │  │
│                           └─────────────┬───────────────┘  │
└─────────────────────────────────────────┼───────────────────┘
                                          │
                                          ▼
                              ┌─────────────────────┐
                              │  Nexus Server       │
                              │  - File System      │
                              │  - Memory API       │
                              │  - Search           │
                              └─────────────────────┘
```

### ReAct Flow (Automatic)

Unlike LangGraph where you manually build the ReAct loop, OpenAI Agents SDK handles it automatically:

```
User provides task
       │
       ▼
Agent.run() or Runner.run()
       │
       ▼
┌──────────────────────────────┐
│ Built-in ReAct Loop          │
│                              │
│ 1. LLM reasons about task    │
│ 2. Decides which tool to call│
│ 3. Executes tool             │
│ 4. Observes results          │
│ 5. Repeats until complete    │
└──────────────────────────────┘
       │
       ▼
Final response
```

## Tool Documentation

### File Operations

#### grep_files

Search file content using patterns.

```python
@function_tool
async def grep_files(pattern: str, path: str = "/", case_sensitive: bool = True) -> str:
    """Search file content using grep-style patterns."""
```

**Examples:**
- `grep_files("async def", "/workspace")` → Find async functions
- `grep_files("TODO:", "/")` → Find all TODO comments
- `grep_files("import pandas", "/scripts", False)` → Case-insensitive search

#### glob_files

Find files by name pattern.

```python
@function_tool
async def glob_files(pattern: str, path: str = "/") -> str:
    """Find files by name pattern using glob syntax."""
```

**Examples:**
- `glob_files("*.py", "/workspace")` → All Python files
- `glob_files("**/*.md", "/docs")` → All Markdown files recursively
- `glob_files("test_*.py", "/tests")` → All test files

#### read_file

Read file content.

```python
@function_tool
async def read_file(path: str, preview: bool = False) -> str:
    """Read file content (full or preview mode)."""
```

**Examples:**
- `read_file("/workspace/README.md")` → Read entire file
- `read_file("/scripts/large.py", True)` → Preview first 100 lines

#### write_file

Write content to file.

```python
@function_tool
async def write_file(path: str, content: str) -> str:
    """Write content to Nexus filesystem."""
```

**Examples:**
- `write_file("/reports/summary.md", "# Summary\n...")` → Save report

### Memory Operations

#### store_memory

Store information in persistent memory.

```python
@function_tool
async def store_memory(content: str, memory_type: str = "fact") -> str:
    """Store information in persistent memory."""
```

**Memory Types:**
- `"fact"` - Factual information
- `"preference"` - User preferences
- `"experience"` - Past events/actions

#### recall_memory

Query stored memories.

```python
@function_tool
async def recall_memory(query: str, limit: int = 5) -> str:
    """Query stored memories using semantic search."""
```

#### list_all_memories

List all stored memories.

```python
@function_tool
async def list_all_memories() -> str:
    """List all stored memories."""
```

## Configuration

### Remote Nexus Server

By default, connects to demo server at `http://136.117.224.98`. Override with:

```bash
export NEXUS_SERVER_URL="http://your-server:8080"
export NEXUS_API_KEY="your-api-key"
```

### Multi-Tenancy Support

Nexus supports multi-tenancy for data isolation:

```bash
# Set tenant and agent identifiers
export NEXUS_TENANT_ID="my-team"
export NEXUS_AGENT_ID="file-analyzer"
```

**Common Tenant Patterns:**

| Pattern | Example | Use Case |
|---------|---------|----------|
| Demo/Testing | `openai-agents-demo` | Development |
| Per-User | `user-{user_id}` | SaaS applications |
| Per-Team | `team-{team_name}` | Collaboration |
| Per-Environment | `prod-workflow` | Production isolation |

### Local Testing

To test with a local Nexus server:

```bash
# Terminal 1: Start server
nexus serve --port 8080

# Terminal 2: Run demos
export NEXUS_SERVER_URL="http://localhost:8080"
python openai_agent_react_demo.py
```

## Use Cases

### 1. Code Analysis Agent

Search codebases, analyze patterns, generate documentation.

```python
agent = Agent(
    name="CodeAnalyzer",
    instructions="Analyze code structure and patterns",
    tools=[grep_files, glob_files, read_file, write_file]
)

result = agent.run(
    "Find all async patterns and create documentation"
)
```

### 2. Documentation Generator

Automatically generate docs from code.

```python
agent.run(
    "Find all API endpoint definitions and create API documentation"
)
```

### 3. TODO/FIXME Tracker

Scan for TODO comments and create task lists.

```python
agent.run(
    "Find all TODO and FIXME comments and create a prioritized task list"
)
```

### 4. Research Agent with Memory

Gather information and remember findings.

```python
agent = Agent(
    name="ResearchAgent",
    instructions="Research topics and remember findings",
    tools=[grep_files, read_file, store_memory, recall_memory]
)

agent.run("Research error handling patterns and remember key insights")
```

### 5. Multi-Agent Collaboration

Multiple agents sharing a Nexus workspace.

```python
# Agent 1: Code analyzer
analyzer = Agent(
    name="Analyzer",
    tools=get_nexus_tools(nx),
)

# Agent 2: Documentation writer
writer = Agent(
    name="Writer",
    tools=get_nexus_tools(nx),
)

# Agents can read each other's outputs via shared filesystem
```

## Comparison with Other Frameworks

See [COMPARISON.md](COMPARISON.md) for detailed comparison with:
- LangGraph + Nexus
- Claude Agent SDK + Nexus
- OpenAI Agents SDK + Nexus

**Quick Summary:**

| Framework | Code Lines | ReAct Loop | Multi-LLM |
|-----------|-----------|------------|-----------|
| **LangGraph** | ~370 | Manual | ✅ Yes |
| **Claude Agent SDK** | ~280 | Automatic | ❌ Claude only |
| **OpenAI Agents SDK** | ~300 | Automatic | ❌ OpenAI only |

**All three implement the same ReAct pattern with Nexus integration!**

## Troubleshooting

### "OPENAI_API_KEY not found"

Set your OpenAI API key:

```bash
export OPENAI_API_KEY="sk-..."
```

### "Error connecting to Nexus"

1. Check server is running: `curl http://136.117.224.98:8080/health`
2. Try local server: `nexus serve`
3. Set custom URL: `export NEXUS_SERVER_URL="http://localhost:8080"`

### "Module not found"

Install dependencies:

```bash
pip install -r requirements.txt
```

### "Tool execution failed"

1. Check file paths are absolute (start with `/`)
2. Verify Nexus server is accessible
3. Check API key if using remote server

## Learning Resources

- **OpenAI Agents SDK**: https://openai.github.io/openai-agents-python/
- **Nexus Documentation**: https://github.com/nexi-lab/nexus
- **LangGraph Comparison**: [../langgraph/README.md](../langgraph/README.md)
- **Claude Agent SDK Comparison**: [../claude_agent_sdk/README.md](../claude_agent_sdk/README.md)

## Next Steps

1. **Try the demos** - Run both examples to see ReAct in action
2. **Read COMPARISON.md** - Understand differences between frameworks
3. **Build your own** - Use the patterns as templates
4. **Explore Nexus features**:
   - Memory API for persistence
   - Workflows for automation
   - Semantic search for RAG
   - Multi-tenancy for SaaS

## Advanced: Custom Tools

Create your own tools by decorating functions:

```python
from agents import function_tool

@function_tool
async def custom_analysis(code: str) -> str:
    """Analyze code and return insights.

    Args:
        code: Source code to analyze
    """
    # Your custom logic here
    return "Analysis results..."

# Add to agent
agent = Agent(
    name="CustomAgent",
    tools=[custom_analysis, *get_nexus_tools(nx)]
)
```

## License

Apache-2.0 (same as Nexus)
