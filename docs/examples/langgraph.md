# LangGraph + Nexus Integration

Build powerful ReAct agents with LangGraph that interact with a persistent Nexus filesystem. Enable code analysis, documentation generation, and intelligent file operations across your entire codebase.

## 🎯 What is LangGraph + Nexus?

**LangGraph** is a framework for building stateful, graph-based AI workflows. Combined with **Nexus**, it provides:

- **Persistent filesystem access** - Agents can search, read, and write files
- **Remote execution** - Work with files on shared Nexus servers
- **Multi-tenancy support** - Isolated workspaces for different teams/agents
- **Familiar tools** - grep, glob, cat/less, and write operations
- **ReAct architecture** - Agents reason, act, and observe in a loop

Nexus provides the infrastructure layer for LangGraph agents to:

- **Search codebases** with pattern matching
- **Analyze files** with full content access
- **Generate reports** that persist across sessions
- **Coordinate** through shared filesystem namespaces

## 📊 Demo: ReAct Agent with File Operations

The LangGraph demo shows an agent performing code analysis tasks using familiar command-line tools adapted for AI agents.

### What the Demo Shows

A ReAct agent that:

1. **Searches** for code patterns using grep
2. **Finds** files by name using glob patterns
3. **Reads** file content using cat/less commands
4. **Writes** analysis reports to the filesystem
5. **Reasons** about multi-step tasks autonomously

### Quick Start

```bash
# Install dependencies
cd examples/langgraph
pip install -r requirements.txt

# Set your LLM API key (choose one)
export OPENROUTER_API_KEY="sk-or-v1-..."  # Recommended
# or
export ANTHROPIC_API_KEY="sk-ant-..."
# or
export OPENAI_API_KEY="sk-..."

# Optional: Populate test data
python setup_test_data.py

# Run the demo
python langgraph_react_demo.py
```

!!! tip "Multi-LLM Support"
    The demo works with Claude (Anthropic), GPT-4 (OpenAI), or any model via OpenRouter. It automatically detects which API key is available.

## 🔬 How It Works

### The ReAct Loop

```mermaid
graph TB
    Start([🎯 Task: Find async patterns]) --> Think[🤔 Think<br/>Agent reasons about approach]

    Think --> Act1[⚙️ Act: grep_files<br/>"async def /workspace"]

    Act1 --> Observe1[👀 Observe<br/>Found 12 matches]

    Observe1 --> Think2[🤔 Think<br/>Need to read examples]

    Think2 --> Act2[⚙️ Act: read_file<br/>"cat /workspace/api.py"]

    Act2 --> Observe2[👀 Observe<br/>See async patterns]

    Observe2 --> Think3[🤔 Think<br/>Ready to write report]

    Think3 --> Act3[⚙️ Act: write_file<br/>"/reports/analysis.md"]

    Act3 --> Done([✅ Complete])

    style Start fill:#e1f5ff,stroke:#0288d1,stroke-width:3px
    style Think fill:#fce4ec,stroke:#c2185b,stroke-width:2px
    style Think2 fill:#fce4ec,stroke:#c2185b,stroke-width:2px
    style Think3 fill:#fce4ec,stroke:#c2185b,stroke-width:2px
    style Act1 fill:#e8f5e9,stroke:#388e3c,stroke-width:2px
    style Act2 fill:#e8f5e9,stroke:#388e3c,stroke-width:2px
    style Act3 fill:#e8f5e9,stroke:#388e3c,stroke-width:2px
    style Done fill:#a5d6a7,stroke:#2e7d32,stroke-width:3px
```

### Nexus File Tools

The demo provides four tools that wrap Nexus filesystem operations:

#### 1. grep_files - Content Search

Search file content using grep-style patterns:

```python
from nexus_tools import get_nexus_tools
import nexus

# Connect to Nexus
nx = nexus.connect(remote_url="http://nexus-server:8080")

# Create tools
tools = get_nexus_tools(nx)

# Agent uses grep_files tool
result = grep_files("async def /workspace")
# Returns:
# Found 12 matches for pattern 'async def' in /workspace:
#
# /workspace/api.py:
#   Line 45: async def fetch_data(url: str):
#   Line 67: async def process_batch(items: list):
```

#### 2. glob_files - Name-based Search

Find files by pattern matching:

```python
# Find all Python files
result = glob_files("*.py", "/workspace")

# Find Markdown docs recursively
result = glob_files("**/*.md", "/docs")

# Find test files
result = glob_files("test_*.py", "/tests")
```

#### 3. read_file - Content Reading

Read file contents with cat/less:

```python
# Read entire file
content = read_file("cat /workspace/config.yaml")

# Preview large file (first 100 lines)
preview = read_file("less /data/large_file.csv")

# Default to cat
content = read_file("/README.md")
```

#### 4. write_file - Content Writing

Save analysis results and reports:

```python
# Write analysis report
write_file("/reports/summary.md", "# Analysis\n\nFindings...")

# Save configuration
write_file("/config/settings.json", "{}")
```

## 🛠️ Architecture

### Component Overview

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
                              │  Multi-tenant        │
                              │  Persistent storage  │
                              └─────────────────────┘
```

### Multi-Tenancy Pattern

Nexus supports isolated workspaces for different teams:

```python
# Development team agent
dev_nx = nexus.connect(
    remote_url="http://nexus-server:8080",
    config={"tenant_id": "team-dev", "agent_id": "code-analyzer"}
)

# QA team agent
qa_nx = nexus.connect(
    remote_url="http://nexus-server:8080",
    config={"tenant_id": "team-qa", "agent_id": "test-validator"}
)

# Shared documentation
docs_nx = nexus.connect(
    remote_url="http://nexus-server:8080",
    config={"tenant_id": "shared-docs", "agent_id": "doc-generator"}
)
```

Benefits:
- ✅ **Data Isolation** - Each tenant's data is separate
- ✅ **Shared Infrastructure** - One server, multiple teams
- ✅ **Access Control** - Tenant-scoped permissions
- ✅ **Cost Efficiency** - Shared resources

## 📈 Example Tasks

### 1. Code Analysis

Find and analyze async/await patterns:

```python
from langgraph_react_demo import create_react_agent
import nexus

nx = nexus.connect(remote_url="http://nexus-server:8080")
agent = create_react_agent(nx)

result = agent.invoke({
    "messages": [{
        "role": "user",
        "content": "Find all async/await usage, analyze patterns, and create a summary report"
    }]
})
```

**Agent automatically:**
1. Uses `grep_files("async def /workspace")` to find matches
2. Uses `read_file()` to examine specific files
3. Uses `write_file()` to save analysis report

### 2. Documentation Generation

Generate API documentation from code:

```python
result = agent.invoke({
    "messages": [{
        "role": "user",
        "content": "Find all API endpoint definitions and create API documentation"
    }]
})
```

**Agent performs:**
1. `glob_files("**/routes/*.py")` - Find route files
2. `grep_files("@app.route")` - Find endpoints
3. `read_file()` - Read endpoint implementations
4. `write_file("/docs/api.md")` - Generate docs

### 3. TODO Tracker

Scan for TODO comments and create task list:

```python
result = agent.invoke({
    "messages": [{
        "role": "user",
        "content": "Find all TODO and FIXME comments and create a prioritized task list"
    }]
})
```

### 4. Security Audit

Find potential security issues:

```python
result = agent.invoke({
    "messages": [{
        "role": "user",
        "content": "Search for hardcoded credentials, SQL injection risks, and XSS vulnerabilities"
    }]
})
```

## 💡 Real-World Applications

### Code Review Assistant

Agent reviews pull requests and provides feedback:

```python
# Agent analyzes changed files
agent.invoke({
    "messages": [{
        "role": "user",
        "content": "Review the changes in /pr/123 and check for code quality issues"
    }]
})

# Results saved to /reviews/pr-123.md
```

### Knowledge Base Generator

Build documentation from codebase:

```python
# Agent creates comprehensive docs
agent.invoke({
    "messages": [{
        "role": "user",
        "content": "Generate developer onboarding guide covering architecture, setup, and key components"
    }]
})

# Results in /docs/onboarding.md
```

### Dependency Auditor

Track and analyze dependencies:

```python
agent.invoke({
    "messages": [{
        "role": "user",
        "content": "Find all import statements, identify external dependencies, and check for updates"
    }]
})
```

### Migration Assistant

Help migrate code patterns:

```python
agent.invoke({
    "messages": [{
        "role": "user",
        "content": "Find all class-based views and suggest FastAPI equivalents"
    }]
})
```

## 🎓 Customization

### Add Custom Tools

Extend the agent with new capabilities:

```python
from langchain_core.tools import tool

@tool
def semantic_search(query: str, path: str = "/") -> str:
    """Search files by semantic meaning."""
    results = nx.search(path, query=query)
    return format_search_results(results)

# Add to tool list
def get_nexus_tools(nx):
    # ... existing tools ...
    return [grep_files, glob_files, read_file, write_file, semantic_search]
```

### Modify Task Examples

Edit tasks in `langgraph_react_demo.py`:

```python
tasks = [
    {
        "name": "Security Audit",
        "prompt": "Find potential security vulnerabilities including hardcoded secrets, SQL injection risks, and XSS issues. Create a detailed security report.",
    },
    {
        "name": "Performance Analysis",
        "prompt": "Identify performance bottlenecks, expensive operations, and optimization opportunities.",
    },
]
```

### Configure LLM Settings

Adjust model parameters:

```python
from langchain_anthropic import ChatAnthropic

llm = ChatAnthropic(
    model="claude-3-5-sonnet-20241022",
    temperature=0.1,  # Lower = more deterministic
    max_tokens=8192,  # Longer responses
)
```

## 🔌 Configuration

### Remote Nexus Server

Connect to your Nexus server:

```bash
export NEXUS_SERVER_URL="http://your-server:8080"
export NEXUS_API_KEY="your-api-key"  # Optional
```

Or configure in code:

```python
nx = nexus.connect(
    remote_url="http://your-server:8080",
    api_key="your-api-key"
)
```

### Multi-Tenancy

Set tenant and agent identifiers:

```bash
export NEXUS_TENANT_ID="my-team"
export NEXUS_AGENT_ID="code-analyzer"
```

Or in code:

```python
nx = nexus.connect(
    remote_url="http://server:8080",
    config={
        "tenant_id": "my-team",
        "agent_id": "code-analyzer"
    }
)
```

### Local Development

Test with local Nexus:

```bash
# Terminal 1: Start server
nexus serve --host 0.0.0.0 --port 8080

# Terminal 2: Run demo
export NEXUS_SERVER_URL="http://localhost:8080"
python langgraph_react_demo.py
```

## 📚 Learn More

- **LangGraph Docs**: https://langchain-ai.github.io/langgraph/
- **ReAct Paper**: https://arxiv.org/abs/2210.03629
- **Nexus Documentation**: [../getting-started/quickstart.md](../getting-started/quickstart.md)
- **Example Code**: [examples/langgraph/](https://github.com/nexi-lab/nexus/tree/main/examples/langgraph)

## 🚀 Next Steps

1. **Run the demo** - See the agent in action
2. **Customize tools** - Add your own file operations
3. **Build workflows** - Chain multiple agents together
4. **Deploy to production** - Scale with LangGraph Cloud or Kubernetes

---

**Powered by LangGraph + Nexus** 🔗 - Infrastructure for intelligent agents
