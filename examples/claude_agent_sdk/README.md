# Claude Agent SDK + Nexus Integration Examples

This directory contains examples demonstrating how to integrate [Anthropic's Claude Agent SDK for Python](https://github.com/anthropics/claude-agent-sdk-python) with Nexus filesystem.

## What is Claude Agent SDK?

Claude Agent SDK is Anthropic's official Python SDK for building agentic applications with Claude. It provides:

- **Built-in ReAct loop** - Automatic reasoning and tool calling
- **Custom tools** - Define Python functions as in-process MCP servers
- **Streaming** - Real-time response streaming
- **Hooks** - Intercept and control agent execution
- **Async-first** - Native async/await support

## Why Claude Agent SDK + Nexus?

Combining Claude Agent SDK with Nexus gives you:

1. **Persistent Memory** - Agents can store/recall information across sessions using Nexus Memory API
2. **File Operations** - Agents can read, write, search files in a versioned, multi-tenant filesystem
3. **Semantic Search** - Agents can query documents with natural language
4. **Workflow Automation** - Trigger Claude agents from file events
5. **Multi-tenancy** - Isolated agent workspaces per user/team

## Examples

### 1. ReAct Demo (`claude_agent_react_demo.py`)

A complete ReAct agent that uses Nexus for file operations. Demonstrates:

- Searching files with `grep_files()`
- Finding files with `glob_files()`
- Reading files with `read_file()`
- Writing results with `write_file()`

**Run it:**
```bash
export ANTHROPIC_API_KEY="your-key"
python claude_agent_react_demo.py
```

**Tasks it can do:**
- Find all Python files with async/await and write a summary
- Search for TODO comments and generate a task list
- Analyze code structure and write documentation

### 2. Comparison Guide (`COMPARISON.md`)

Detailed comparison between LangGraph ReAct and Claude Agent SDK approaches. Shows:

- Architecture differences
- Code comparison (same task, 70% less code with Claude SDK)
- When to use which framework
- Performance considerations

## Integration Patterns

### Pattern 1: Nexus Tools for Claude Agent SDK

Expose Nexus operations as tools that Claude can invoke:

```python
from claude_agent_sdk import query
import nexus

nx = nexus.connect()

# Define tools as async functions
async def read_file(path: str) -> str:
    """Read file from Nexus"""
    return nx.read(path).decode('utf-8')

async def write_file(path: str, content: str) -> str:
    """Write file to Nexus"""
    nx.write(path, content.encode('utf-8'))
    return f"Wrote to {path}"

# Claude automatically uses tools as needed
async for message in query(
    "Read /workspace/data.json and create a summary in /reports/summary.md",
    tools=[read_file, write_file]
):
    print(message)
```

### Pattern 2: Nexus Memory as Agent Memory

Use Nexus Memory API for persistent agent knowledge:

```python
import nexus
from claude_agent_sdk import query

nx = nexus.connect()

async def store_memory(content: str, memory_type: str = "fact") -> str:
    """Store information in agent memory"""
    nx.memory.store(
        content=content,
        scope="agent",
        memory_type=memory_type,
        importance=0.8
    )
    return "Memory stored"

async def recall_memory(query_text: str) -> str:
    """Recall relevant information from memory"""
    results = nx.memory.query(query_text, scope="agent", limit=5)
    return "\n".join([m.content for m in results])

# Agent can now remember across conversations
async for msg in query(
    "What did we discuss about the API design last week?",
    tools=[store_memory, recall_memory]
):
    print(msg)
```

### Pattern 3: Nexus LLM Read for Document Analysis

Use Nexus's built-in LLM document reading:

```python
import nexus
from claude_agent_sdk import query

nx = nexus.connect()

async def analyze_document(path: str, question: str) -> str:
    """Ask questions about documents using Nexus LLM read"""
    # Nexus handles parsing (PDF, DOCX, etc.) and citation
    answer = nx.llm_read(path, question)
    return answer

# Claude can analyze complex documents
async for msg in query(
    "Analyze all PDFs in /contracts and summarize key terms",
    tools=[analyze_document, glob_files]
):
    print(msg)
```

### Pattern 4: Workflow-Triggered Agents

Trigger Claude agents from Nexus workflows:

```yaml
# .nexus/workflows/review-code.yml
name: Auto Code Review
trigger:
  event: FILE_WRITE
  pattern: "**/*.py"
actions:
  - type: python
    code: |
      from claude_agent_sdk import query
      import nexus

      nx = nexus.connect()
      code = nx.read(event.path).decode('utf-8')

      async for msg in query(
          f"Review this Python code for bugs and style issues:\n\n{code}",
          tools=[write_file]
      ):
          # Write review to /reviews/{filename}.md
          pass
```

## Quick Start

See **[QUICK_START.md](./QUICK_START.md)** for a 5-minute setup guide!

**TL;DR**:
```bash
# Install
pip install nexus-ai-fs claude-agent-sdk
export ANTHROPIC_API_KEY="your-key"

# Option 1: Local (no server needed)
python claude_agent_react_demo.py

# Option 2: With server
./start_server.sh                    # Terminal 1
python claude_agent_react_demo.py    # Terminal 2
```

## Installation

```bash
# Install Nexus
pip install nexus-ai-fs

# Install Claude Agent SDK
pip install claude-agent-sdk

# Or install both
pip install nexus-ai-fs claude-agent-sdk
```

## Requirements

- **Python 3.10+** (required by Claude Agent SDK)
- **Node.js** (required by Claude Agent SDK)
- **Claude Code 2.0.0+** (install via npm)
- **Anthropic API key**

```bash
# Set your Anthropic API key
export ANTHROPIC_API_KEY="your-key"

# Optional: For remote Nexus server
export NEXUS_API_KEY="your-nexus-key"
export NEXUS_SERVER_URL="http://your-server:8080"
```

## Key Differences from LangGraph

| Aspect | Claude Agent SDK | LangGraph |
|--------|-----------------|-----------|
| **Setup** | `query(prompt, tools=[...])` | Build StateGraph with nodes/edges |
| **Code** | ~30 lines | ~100 lines |
| **Tools** | Plain async functions | `@tool` decorated functions |
| **LLM** | Claude (Anthropic) | Any LangChain LLM |
| **Loop** | Built-in ReAct | Manual graph definition |
| **Streaming** | Async generator | State-based |

**Bottom line**: Use Claude Agent SDK for simpler code and faster prototyping. Use LangGraph for multi-LLM support and complex flows.

## Advanced Use Cases

### 1. Multi-Agent System with Nexus

Each agent has its own workspace in Nexus:

```python
from claude_agent_sdk import ClaudeSDKClient
import nexus

# Agent 1: Researcher
nx1 = nexus.connect()
nx1.tenant_id = "research-team"
nx1.agent_id = "researcher-001"

# Agent 2: Writer
nx2 = nexus.connect()
nx2.tenant_id = "research-team"
nx2.agent_id = "writer-001"

# They can share files in /shared/ but have private workspaces
```

### 2. Agent with Semantic Search

```python
async def search_documents(query: str, path: str = "/workspace") -> str:
    """Search documents using semantic similarity"""
    results = nx.semantic_search(query, path=path, limit=10)
    return "\n".join([f"{r.path}: {r.snippet}" for r in results])

# Claude can find relevant docs even if keywords don't match
async for msg in query(
    "Find documents about machine learning algorithms",
    tools=[search_documents, read_file]
):
    print(msg)
```

### 3. Agent with Version Control

```python
async def get_file_versions(path: str) -> str:
    """Get version history of a file"""
    versions = nx.list_versions(path)
    return "\n".join([f"v{v.version} ({v.timestamp}): {v.hash}" for v in versions])

async def read_version(path: str, version: int) -> str:
    """Read specific version of a file"""
    content = nx.read(path, version=version)
    return content.decode('utf-8')

# Claude can time-travel through file history
```

## Additional Resources

- [Claude Agent SDK GitHub](https://github.com/anthropics/claude-agent-sdk-python)
- [Nexus Documentation](https://github.com/nexi-lab/nexus)
- [Comparison: LangGraph vs Claude Agent SDK](./COMPARISON.md)
- [LangGraph ReAct Example](../langgraph/langgraph_react_demo.py)

## Support

For issues:
- Claude Agent SDK: https://github.com/anthropics/claude-agent-sdk-python/issues
- Nexus: https://github.com/nexi-lab/nexus/issues

## License

Same as parent Nexus project.
