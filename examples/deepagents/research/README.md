# Research Agent Examples with Nexus

This directory contains examples of using Nexus as the filesystem backend for DeepAgents research agents.

These examples are based on the [DeepAgents research agent example](https://github.com/langchain-ai/deepagents/tree/master/examples/research).

## Setup

### 1. Install Dependencies

```bash
cd examples/deepagents
pip install -r requirements.txt
```

### 2. Set API Keys

```bash
# Required: Anthropic API key for Claude
export ANTHROPIC_API_KEY="your-anthropic-api-key"

# Required for internet search: Tavily API key
export TAVILY_API_KEY="your-tavily-api-key"
```

Get API keys:
- Anthropic: https://console.anthropic.com
- Tavily: https://tavily.com

## Demos

### Demo 1: Drop-In Replacement (Tier 1)

**File:** `demo_1_drop_in.py`

Shows basic Nexus integration as a drop-in filesystem replacement.

**What you get:**
- ✅ Automatic versioning of research reports
- ✅ Persistent workspace across sessions
- ✅ Audit trail of agent operations
- ✅ Time-travel debugging

**Run it:**
```bash
python research/demo_1_drop_in.py
```

**What happens:**
1. Agent researches a topic using internet search
2. Writes findings to `final_report.md` in Nexus
3. Shows version history (if report was edited)
4. Shows audit trail of operations

**Key takeaway:** Agent works identically, but you get production-ready features for free.

---

### Demo 2: Semantic Research (Tier 2) - Coming Soon

**File:** `demo_2_semantic_research.py` (planned)

Shows how agents can find related past research using semantic search.

**What you get:**
- Agent automatically finds relevant previous research
- Builds on existing knowledge instead of starting from scratch
- Uses `nexus_semantic_search` tool

---

### Demo 3: Cumulative Learning (Tier 2) - Coming Soon

**File:** `demo_3_cumulative_learning.py` (planned)

Shows how agents remember insights across research sessions.

**What you get:**
- Agent stores key insights in Nexus Memory API
- Recalls relevant knowledge in future research
- Builds cumulative knowledge over time

---

### Demo 4: Multi-Document Analysis (Tier 2) - Coming Soon

**File:** `demo_4_multi_doc_analysis.py` (planned)

Shows how agents analyze many documents at once.

**What you get:**
- Agent uses `nexus_llm_read` to analyze 100s of papers
- Processes documents that wouldn't fit in context window
- Synthesizes findings across large corpus

---

## Understanding the Demos

### What's Different from Default DeepAgents?

**Default DeepAgents:**
```python
agent = create_deep_agent(
    model="anthropic:claude-sonnet-4-20250514",
    # Uses default local filesystem
)
```

**With Nexus (Demo 1):**
```python
import nexus
from nexus_backend import NexusBackend

nx = nexus.connect()

agent = create_deep_agent(
    model="anthropic:claude-sonnet-4-20250514",
    backend=NexusBackend(nx)  # Just pass backend!
)
```

**Just 2 lines changed** - but you get versioning, persistence, audit trail!

### Tier 1 vs Tier 2

**Tier 1 (Drop-in):**
- Same agent behavior
- Better ops/debugging/deployment
- Infrastructure improvements

**Tier 2 (Enhanced):**
- New agent capabilities
- Semantic search, memory, multi-doc analysis
- Agent gets smarter

## How It Works

### Architecture

```
Research Agent
    │
    ├─ internet_search tool (Tavily)
    │
    └─ FilesystemMiddleware(backend=NexusBackend)
        ├─ ls → nx.list()
        ├─ read_file → nx.read()
        ├─ write_file → nx.write() [auto-versioned!]
        └─ edit_file → nx.read() + replace + nx.write()
```

All file operations go through Nexus, which provides:
- Automatic versioning
- Audit logging
- Persistence (local or remote)
- Time-travel debugging

### File Structure in Nexus

After running the research agent:

```
/research-demo/
  question.txt          # Original research question
  final_report.md       # Generated report (may have multiple versions)
```

Each file is automatically versioned:
```python
versions = nx.list_versions("/research-demo/final_report.md")
# Returns: list of version dicts if agent edited the report
```

## Troubleshooting

### Import Errors

If you see `ModuleNotFoundError: No module named 'deepagents'`:

```bash
pip install deepagents
```

### API Key Errors

If you see `Error: ANTHROPIC_API_KEY not set`:

```bash
export ANTHROPIC_API_KEY="your-key-here"
```

### Tavily Search Errors

The agent can still run without Tavily, but won't be able to search the internet.

To fix:
```bash
export TAVILY_API_KEY="your-key-here"
```

## Next Steps

1. Run `demo_1_drop_in.py` to see basic integration
2. Try running multiple research sessions to see persistence
3. Examine version history after agent edits reports
4. Deploy to remote Nexus server for distributed agents

## Related

- [DeepAgents Documentation](https://deepwiki.com/langchain-ai/deepagents)
- [Nexus Backend Implementation](../nexus_backend.py)
- [Integration Discussion (Issue #308)](https://github.com/nexi-lab/nexus/issues/308)
