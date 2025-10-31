# CrewAI Agents with Nexus Filesystem

This example demonstrates how to build **multi-agent AI systems** using CrewAI that interact with a Nexus filesystem via the Model Context Protocol (MCP). The agents can collaborate, persist knowledge, and work with shared storage across sessions.

## What is CrewAI?

**CrewAI** is a Python framework for orchestrating autonomous AI agents that work collaboratively on complex tasks. Key concepts:

- **Agents** - AI entities with specific roles, goals, and backstories
- **Tasks** - Work assignments with descriptions and expected outputs
- **Crews** - Teams of agents working together on related tasks
- **Tools** - Functions agents use to interact with external systems

## Why CrewAI + Nexus?

This integration brings together:

- **CrewAI's agent orchestration** - Role-based collaboration and task delegation
- **Nexus's AI-native filesystem** - Persistent storage, memory, and permissions
- **MCP Protocol** - Standard interface for tool discovery and execution

## Features

- **Remote Nexus Server**: Connect to a shared Nexus server for persistent storage
- **MCP Integration**: Automatic tool discovery via Model Context Protocol
- **8 Nexus Tools**:
  - `read_file` - Read file content from Nexus
  - `write_file` - Write files to Nexus
  - `list_files` - List directory contents
  - `glob_files` - Find files by pattern (*.py, **/*.md)
  - `grep_files` - Search file contents with regex
  - `semantic_search` - Natural language file search
  - `store_memory` - Persist agent learnings
  - `query_memory` - Retrieve relevant memories
- **Multi-Agent Collaboration**: Agents share data via Nexus filesystem
- **Memory Persistence**: Long-term learning across sessions
- **Three Demo Scenarios**: File analysis, research with memory, multi-agent workflows

## Files

- **`crewai_nexus_demo.py`** - Main demo with 3 scenarios (file analysis, memory, collaboration)
- **`start_nexus_server.sh`** - Script to start local Nexus server
- **`run_demo.sh`** - Wrapper script to run demos with environment checks
- **`requirements.txt`** - Python dependencies (CrewAI, LLM providers)
- **`README.md`** - This comprehensive documentation

## Quick Start

### 1. Install Dependencies

```bash
cd nexus/examples/crewai
pip install -r requirements.txt
```

### 2. Set API Keys

Choose one LLM provider:

```bash
# Option 1: Anthropic (recommended for Claude)
export ANTHROPIC_API_KEY="sk-ant-..."

# Option 2: OpenAI (for GPT-4)
export OPENAI_API_KEY="sk-..."

# Option 3: OpenRouter (access to all models)
export OPENROUTER_API_KEY="sk-or-v1-..."
```

### 3. Start Nexus Server

```bash
# Terminal 1: Start the server
./start_nexus_server.sh
```

The server will run on `http://localhost:8080` by default.

### 4. Run the Demo

```bash
# Terminal 2: Run the demo
./run_demo.sh

# Or run directly with Python
python crewai_nexus_demo.py
```

## Demo Scenarios

### Demo 1: File Analysis Agent

A code analyst agent that:
1. Finds Python files using glob patterns
2. Searches for async/await patterns using grep
3. Reads example files
4. Creates a summary report

**Tools used**: `glob_files`, `grep_files`, `read_file`, `write_file`

```bash
./run_demo.sh 1
```

### Demo 2: Research Agent with Memory

A researcher agent that:
1. Uses semantic search to find relevant documents
2. Reads and analyzes the content
3. Stores key insights in persistent memory
4. Recalls memories in subsequent tasks

**Tools used**: `semantic_search`, `read_file`, `store_memory`, `query_memory`

**Key Feature**: Memories persist across runs, enabling long-term learning!

```bash
./run_demo.sh 2
```

### Demo 3: Multi-Agent Collaboration

Two agents working together:

**Agent 1 - Data Collector**:
- Searches for TODO comments
- Organizes findings
- Writes to shared file

**Agent 2 - Analyst**:
- Reads collected data
- Categorizes and prioritizes
- Generates recommendations
- Stores insights in memory

**Tools used**: All 8 tools in coordinated workflow

```bash
./run_demo.sh 3
```

## Example Output

```
========================================================================
CrewAI + Nexus MCP Integration Demo
========================================================================

Checking environment...
✓ Using Anthropic API (Claude)

Nexus server: http://localhost:8080
✓ Connected to Nexus server

✓ Environment check passed!

Setting up test data...
✓ Created 3 test files in /workspace

Available demos:
  1. File Analysis
  2. Research with Memory
  3. Multi-Agent Collaboration
  4. Run all demos

Select demo (1-4, default=1): 1

========================================================================
Demo 1: File Analysis Agent
========================================================================

[Agent - Code Analyst]
I'll analyze Python files in /workspace to find async patterns.

[Tool Call - glob_files]
pattern: *.py, path: /workspace

[Tool Result]
/workspace/api_client.py
/workspace/database.py
/workspace/utils.py

[Tool Call - grep_files]
pattern: async def, path: /workspace

[Tool Result]
/workspace/api_client.py:4: async def fetch_data(url: str) -> dict:
/workspace/api_client.py:10: async def fetch_multiple(urls: list[str]) -> list[dict]:
/workspace/database.py:3: async def get_connection():
/workspace/database.py:7: async def query_users(min_age: int) -> list:

[Tool Call - read_file]
path: /workspace/api_client.py

[Tool Result]
import asyncio
import aiohttp

async def fetch_data(url: str) -> dict:
    '''Fetch data from API endpoint.'''
    ...

[Tool Call - write_file]
path: /reports/async-analysis.md
content: # Async Patterns Analysis...

[Tool Result]
Successfully wrote to /reports/async-analysis.md

[Agent - Final Response]
I've completed the analysis! Found 4 async functions across 2 files:
- api_client.py uses aiohttp for HTTP requests
- database.py uses asyncpg for database queries
Full report saved to /reports/async-analysis.md

========================================================================
Result:
========================================================================
Analysis complete. Report available at /reports/async-analysis.md
```

## Architecture

### Component Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                      CrewAI Framework                        │
│                                                               │
│  ┌──────────────┐         ┌─────────────────────────────┐  │
│  │   Agents     │         │    Nexus Tools (via SDK)    │  │
│  │              │         │  ┌─────────────────────┐    │  │
│  │ - Analyst    │◄────────┤  │ read_file           │    │  │
│  │ - Researcher │         │  │ write_file          │    │  │
│  │ - Collector  │────────►│  │ glob_files          │    │  │
│  │              │         │  │ grep_files          │    │  │
│  └──────────────┘         │  │ semantic_search     │    │  │
│                           │  │ store_memory        │    │  │
│                           │  │ query_memory        │    │  │
│                           │  └─────────────────────┘    │  │
│                           └────────────┬────────────────┘  │
│                                        │                    │
└────────────────────────────────────────┼────────────────────┘
                                         │
                          Nexus SDK (remote_url)
                                         │
                                         ▼
                             ┌─────────────────────┐
                             │  Nexus Server       │
                             │  localhost:8080     │
                             │                     │
                             │  - Filesystem       │
                             │  - Memory API       │
                             │  - Search           │
                             │  - Permissions      │
                             └─────────────────────┘
```

### Data Flow

```
1. CrewAI agent decides to use a tool
       │
       ▼
2. Tool calls Nexus SDK (call_nexus_mcp)
       │
       ▼
3. SDK connects to remote Nexus server
       │
       ▼
4. Server executes operation (read, write, search, memory)
       │
       ▼
5. Result returns to tool
       │
       ▼
6. Tool returns formatted result to agent
       │
       ▼
7. Agent processes result and continues or completes task
```

## Use Cases

### 1. Code Analysis & Documentation

Agents analyze codebases to find patterns, generate documentation, and identify issues.

```python
analyst = Agent(
    role="Code Analyst",
    goal="Analyze code and generate documentation",
    tools=[glob_files, grep_files, read_file, write_file],
)
```

### 2. Research & Knowledge Building

Agents research topics, store learnings in memory, and build knowledge over time.

```python
researcher = Agent(
    role="Research Analyst",
    goal="Research topics and build knowledge base",
    tools=[semantic_search, read_file, store_memory, query_memory],
)
```

### 3. Multi-Agent Collaboration

Specialized agents work together, sharing data via Nexus filesystem.

```python
# Agent 1: Collects data
# Agent 2: Analyzes data
# Agent 3: Generates reports

crew = Crew(
    agents=[collector, analyst, reporter],
    tasks=[collect_task, analyze_task, report_task],
)
```

### 4. Long-Term Learning Systems

Agents remember insights across sessions, improving over time.

```python
# Session 1: Learn about error handling
store_memory("Always use try/except for I/O operations")

# Session 2: Recall previous learnings
memories = query_memory("error handling best practices")
# Agent builds upon previous knowledge
```

### 5. Document Processing Pipelines

Automated workflows that process, analyze, and organize documents.

```python
# Pipeline:
# 1. Glob files (find documents)
# 2. Read contents
# 3. Semantic search for relevance
# 4. Categorize and organize
# 5. Store insights in memory
```

## Configuration

### Nexus Server URL

By default, connects to `http://localhost:8080`. Override with:

```bash
export NEXUS_URL="http://your-server:8080"
```

### Nexus API Key (Optional)

If your server requires authentication:

```bash
export NEXUS_API_KEY="sk-your-api-key"
```

### Custom Data Directory

For the local server:

```bash
export NEXUS_DATA_DIR="./my-data"
./start_nexus_server.sh
```

### Custom Port

```bash
export NEXUS_PORT=8081
./start_nexus_server.sh
```

### Production Database

For production, use PostgreSQL instead of SQLite:

```bash
export NEXUS_DATABASE_URL="postgresql://user:pass@localhost/nexus"
./start_nexus_server.sh
```

## MCP Integration Details

### Current Implementation

This demo uses **direct SDK calls** wrapped as CrewAI tools for reliability:

```python
@tool("Read File")
def read_file(path: str) -> str:
    """Read a file from Nexus filesystem."""
    from nexus import connect
    nx = connect(config={"remote_url": NEXUS_URL})
    return nx.read(path).decode("utf-8")
```

### Alternative: MCPServerAdapter (Future)

When `crewai-tools` MCPServerAdapter is stable, you can use:

```python
from crewai_tools import MCPServerAdapter

# Connect to Nexus MCP server
nexus_mcp = MCPServerAdapter(
    transport="stdio",
    command="nexus",
    args=["mcp", "serve"]
)

# Tools are automatically discovered!
agent = Agent(
    role="Analyst",
    tools=[nexus_mcp],  # All Nexus tools available
)
```

**Benefits**:
- Automatic tool discovery
- Standard MCP protocol
- No custom wrapper code

## Tool Reference

### File Operations

#### read_file(path: str) → str
Read file content from Nexus filesystem.

```python
content = read_file("/workspace/data.txt")
```

#### write_file(path: str, content: str) → str
Write content to a file.

```python
write_file("/reports/summary.md", "# Summary\n\nFindings...")
```

#### list_files(path: str = "/", recursive: bool = False) → str
List files in a directory.

```python
files = list_files("/workspace", recursive=True)
```

### Search Operations

#### glob_files(pattern: str, path: str = "/") → str
Find files matching a glob pattern.

```python
# Find all Python files
py_files = glob_files("*.py", "/workspace")

# Find all markdown recursively
docs = glob_files("**/*.md", "/docs")
```

#### grep_files(pattern: str, path: str = "/") → str
Search file contents using regex.

```python
# Find async functions
results = grep_files("async def", "/workspace")

# Find TODO comments
todos = grep_files("TODO:", "/")
```

#### semantic_search(query: str, limit: int = 10) → str
Natural language file search.

```python
# Find files about authentication
results = semantic_search("authentication and security", limit=5)
```

### Memory Operations

#### store_memory(content: str, memory_type: str = None, importance: float = 0.5) → str
Store a memory for long-term learning.

```python
# Store insight
store_memory(
    "Always validate user input before processing",
    memory_type="security_insight",
    importance=0.9
)
```

#### query_memory(query: str, limit: int = 5) → str
Retrieve relevant memories.

```python
# Recall security insights
memories = query_memory("security best practices", limit=5)
```

## Customization

### Adding Custom Tools

Create new tools following the CrewAI pattern:

```python
from crewai.tools import tool

@tool("Custom Tool")
def my_custom_tool(param: str) -> str:
    """Description of what this tool does."""
    # Implementation
    return result

# Add to agent
agent = Agent(
    role="Custom Agent",
    tools=[read_file, write_file, my_custom_tool],
)
```

### Modifying Agent Behavior

Customize agent roles, goals, and backstories:

```python
agent = Agent(
    role="Senior Code Reviewer",
    goal="Review code for quality and security issues",
    backstory="""You are a senior engineer with 10+ years experience.
    You have a keen eye for security vulnerabilities and performance issues.
    You provide constructive feedback with specific examples.""",
    tools=[grep_files, read_file, write_file, store_memory],
    verbose=True,
    max_iter=15,  # Max iterations
)
```

### Creating New Demo Scenarios

Add new demo functions to `crewai_nexus_demo.py`:

```python
def demo_4_custom_workflow():
    """Your custom demo."""

    agent = Agent(...)
    task = Task(...)
    crew = Crew(agents=[agent], tasks=[task])

    result = crew.kickoff()
    return result

# Add to demos list in main()
demos = [
    ...,
    ("Custom Workflow", demo_4_custom_workflow),
]
```

## Comparison: CrewAI vs LangGraph

| Feature | CrewAI (This Example) | LangGraph (examples/langgraph) |
|---------|----------------------|-------------------------------|
| **Framework** | Role-based agents | State graphs |
| **Complexity** | Higher level, easier | Lower level, more control |
| **Agent Coordination** | Built-in task delegation | Manual state management |
| **Best For** | Business workflows | Custom control flows |
| **Learning Curve** | Easier to start | Steeper but more flexible |
| **Nexus Integration** | Direct SDK calls | Custom tool wrappers |

**Both are excellent choices!** Choose based on your needs:
- **CrewAI**: Business automation, multi-agent teams, role-based workflows
- **LangGraph**: Custom agent architectures, fine control, research applications

## Troubleshooting

### "No API key found"

Set one of the required LLM API keys:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
# or
export OPENAI_API_KEY="sk-..."
# or
export OPENROUTER_API_KEY="sk-or-v1-..."
```

### "Error connecting to Nexus"

1. **Check server is running**:
   ```bash
   curl http://localhost:8080/health
   ```

2. **Start the server**:
   ```bash
   ./start_nexus_server.sh
   ```

3. **Check logs**: Look for errors in Terminal 1

4. **Try different port**:
   ```bash
   export NEXUS_PORT=8081
   ./start_nexus_server.sh
   ```

### "Port already in use"

Another process is using port 8080:

```bash
# Find and stop the process
lsof -ti:8080 | xargs kill

# Or use a different port
export NEXUS_PORT=8081
./start_nexus_server.sh
```

### "Module not found"

Install dependencies:

```bash
pip install -r requirements.txt
```

### Agent getting stuck or looping

1. **Check max_iter**: Increase if task is complex
   ```python
   agent = Agent(..., max_iter=20)
   ```

2. **Simplify task**: Break complex tasks into smaller steps

3. **Add more context**: Provide clearer instructions in task description

### Memory not persisting

Make sure to commit the session after storing:

```python
nx.memory.store(content)
nx.memory.session.commit()  # Important!
```

## Learning Resources

- **CrewAI Docs**: https://docs.crewai.com/
- **CrewAI GitHub**: https://github.com/crewAIInc/crewAI
- **Nexus Docs**: [../../docs/api/README.md](../../docs/api/README.md)
- **MCP Protocol**: https://modelcontextprotocol.io/
- **LangGraph Example**: [../langgraph/README.md](../langgraph/README.md)

## Next Steps

1. **Experiment with the demos** - Modify prompts and see how agents adapt
2. **Add more tools** - Extend with custom Nexus operations
3. **Build your workflow** - Create agents for your specific use case
4. **Enable permissions** - Add ReBAC for multi-tenant scenarios
5. **Deploy to production** - Use PostgreSQL backend and remote Nexus server
6. **Try MCPServerAdapter** - Once stable, migrate to native MCP integration

## Production Deployment

For production use:

1. **Use PostgreSQL backend**:
   ```bash
   export NEXUS_DATABASE_URL="postgresql://user:pass@host/nexus"
   ```

2. **Enable authentication**:
   ```bash
   # Create API keys for agents
   nexus admin create-user agent_1
   nexus admin create-api-key agent_1 --days 365
   ```

3. **Setup permissions**:
   ```bash
   # Grant access to specific paths
   nexus rebac create user agent_1 editor file /workspace/project1
   ```

4. **Scale horizontally**:
   - Multiple Nexus servers behind load balancer
   - Shared PostgreSQL database
   - Redis for caching (optional)

5. **Monitor and observe**:
   - Enable logging
   - Track agent operations
   - Monitor memory usage

## Related Examples

- **[LangGraph Example](../langgraph/)** - ReAct agents with LangGraph
- **[Python SDK Demo](../py_demo/)** - Basic Nexus SDK usage
- **[Memory Demo](../../archive/examples.archive/py_demo/memory_demo.py)** - Memory API examples
- **[Multi-Tenant Demo](../../archive/examples.archive/multi_tenant/)** - Multi-tenancy patterns

## License

Apache-2.0 (same as Nexus)

---

**Built for AI-native collaboration.** [Nexus Docs](../../docs/api/README.md) • [CrewAI Docs](https://docs.crewai.com/) • [Examples](../)
