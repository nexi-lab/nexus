# Nexus Backend for LangChain DeepAgents

This directory contains a Nexus backend adapter for [LangChain DeepAgents](https://github.com/langchain-ai/deepagents), enabling agents to use Nexus as their persistent filesystem layer.

## Two-Tier Value Proposition

### Tier 1: Production Readiness (Drop-in Replacement) ✅

With just `NexusBackend` filesystem swap, you get **infrastructure improvements** for free:

- ✅ **Automatic versioning** - Track how files evolved through agent operations
- ✅ **Persistent workspace** - Agent work survives restarts and process crashes
- ✅ **Remote deployment** - Run agents anywhere, files centralized in Nexus
- ✅ **Audit/observability** - Track what agent wrote, when, and why

**Note:** These don't make the agent smarter - they make it more observable and production-ready.

### Tier 2: Agent Intelligence ✅

With `NexusBackend + Enhanced Tools`, agents get **new capabilities**:

- ✅ **Memory API** - Remember insights across topics and sessions
- ✅ **Cumulative learning** - Agent gets smarter over time, not starting from scratch
- ✅ **Advanced search** - Grep/glob for finding related content
- ✅ **Persistent memories** - Recalls stored across agent restarts

**Usage:**
```python
from nexus_tools import create_nexus_tools

nx = nexus.connect()
agent = create_deep_agent(
    backend=NexusBackend(nx),
    tools=[*create_nexus_tools(nx)]  # Add memory & search tools
)
```

## Quick Start

### Installation

```bash
cd examples/deepagents
pip install -r requirements.txt
```

### Basic Usage

```python
from deepagents import create_deep_agent
import nexus
from nexus_backend import NexusBackend
from nexus_tools import create_nexus_tools  # Tier 2

# Connect to Nexus (embedded or remote)
nx = nexus.connect()

# Tier 1: Basic integration
agent_basic = create_deep_agent(
    model="anthropic:claude-sonnet-4-20250514",
    backend=NexusBackend(nx)  # Just filesystem
)

# Tier 2: With enhanced capabilities
agent_enhanced = create_deep_agent(
    model="anthropic:claude-sonnet-4-20250514",
    backend=NexusBackend(nx),
    tools=[*create_nexus_tools(nx)]  # Add memory & search tools
)

# Use the agent - files are in Nexus!
agent_enhanced.invoke({"messages": [{"role": "user", "content": "Research quantum computing and store key insights"}]})

# Agent can now:
# - Use nexus_store_memory() to remember insights
# - Use nexus_query_memory() to recall past learnings
# - Build on previous research instead of starting over
```

## Architecture

### Backend Protocol Implementation

`NexusBackend` implements the DeepAgents `BackendProtocol`:

- `ls_info(path)` → `nx.list()` + `nx.stat()`
- `read(file_path, offset, limit)` → `nx.read()` with line formatting
- `write(file_path, content)` → `nx.write()` (auto-versioned)
- `edit(file_path, old, new, replace_all)` → `nx.read()` + replace + `nx.write()`
- `glob_info(pattern, path)` → `nx.glob()`
- `grep_raw(pattern, path, glob)` → `nx.read()` + regex search

All file operations are automatically versioned and audited by Nexus.

## Examples

See the `research/` directory for working examples:

- `demo_1_drop_in.py` - Basic integration (Tier 1)
- `demo_2_semantic_research.py` - Semantic search (Tier 2, coming soon)
- `demo_3_cumulative_learning.py` - Cross-session memory (Tier 2, coming soon)
- `demo_4_multi_doc_analysis.py` - Multi-document analysis (Tier 2, coming soon)

## What's Different from Default Filesystem?

| Feature | Default Filesystem | Nexus Backend |
|---------|-------------------|---------------|
| **Versioning** | No version history | Every write creates new version |
| **Persistence** | Local disk only | Local or remote Nexus server |
| **Audit Trail** | No audit log | Full audit trail of agent actions |
| **Debugging** | Can't see past states | Time-travel to any version |
| **Deployment** | Single machine | Deploy anywhere, files centralized |
| **Multi-agent** | No coordination | Permissions and isolation |

## Use Cases

### Research Agent with History Tracking

Track how a research report evolved through critique cycles:

```python
agent.run("Research transformers")
# Agent writes v1, critique edits to v2, v3, etc.

# Later: debug the agent's thinking process
versions = nx.list_versions("/final_report.md")
for i, v in enumerate(versions, 1):
    content = nx.get_version("/final_report.md", i)
    print(f"v{i}: {content}")
```

### Long-Running Research Projects

Continue research across multiple sessions:

```python
# Day 1
agent.run("Start research on AGI safety")
# Writes initial findings to Nexus

# Day 2 (new process)
agent.run("Continue AGI safety research")
# Agent reads previous work from Nexus, builds on it
```

### Distributed Agent Deployment

Deploy agents to cloud with centralized storage:

```python
# Connect to remote Nexus server
nx = nexus.connect("https://nexus.yourcompany.com")

# Agents run anywhere, files in one place
agent = create_deep_agent(
    middleware=[FilesystemMiddleware(backend=NexusBackend(nx))]
)
```

## Implementation Details

### Path Resolution

- Relative paths are resolved against `base_path` (default: `/`)
- Absolute paths are used as-is
- All paths are normalized to Nexus conventions

### Encoding

- Default encoding: UTF-8
- Configurable via `NexusBackend(nx, encoding="utf-8")`

### Error Handling

- File not found: Returns appropriate error messages
- Read errors: Returns formatted error strings
- Write errors: Returns `WriteResult` with error field populated

### Version Control

- Every `write()` creates a new version in Nexus
- Every `edit()` creates a new version
- Access previous versions via `nx.get_version(path, version_num)`

## Testing

```bash
# Run basic integration test
python research/demo_1_drop_in.py

# Verify versioning works
# (after running demo, check versions were created)
```

## Contributing

See the main Nexus repository for contribution guidelines: https://github.com/nexi-lab/nexus

## Related

- [DeepAgents Documentation](https://deepwiki.com/langchain-ai/deepagents)
- [Nexus Documentation](https://nexi-lab.github.io/nexus/)
- [Issue #308: Integration Discussion](https://github.com/nexi-lab/nexus/issues/308)
