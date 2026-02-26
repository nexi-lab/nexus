# Benchmark Methodology

## Overview

This document describes the methodology used to benchmark three tool loading strategies for MCP-based LLM agents.

## Test Environment

### Hardware
- Local development machine (macOS)
- All benchmarks run sequentially to avoid resource contention

### Software
- Python 3.12
- MCP SDK latest
- Anthropic Python SDK with beta headers

## Model Configuration

| Role | Model | Provider | Notes |
|------|-------|----------|-------|
| Agent (Static/Dynamic) | Claude Sonnet 4 | Anthropic | `claude-sonnet-4-20250514` |
| Agent (Tool Search) | Claude Sonnet 4.5 | Anthropic | `claude-sonnet-4-5-20250929` |
| Judge | Claude Opus 4.5 | Anthropic | `claude-opus-4-5-20251101` |

**Why different models?**
- Anthropic Tool Search requires the `advanced-tool-use-2025-11-20` beta header
- This feature works best with Claude Sonnet 4.5
- Static and Dynamic use Claude Sonnet 4 to demonstrate approach-agnostic results

## Dataset

### Task Definitions

Two representative tasks were used:

#### Task 1: OpenAPI Explorer
```json
{
  "task_id": "openapi_explorer_001",
  "description": "Get API overview for AI platform and code hosting service",
  "required_tools": ["OpenAPI Explorer:getApiOverview"],
  "type": "api_exploration"
}
```

#### Task 2: Calculator Operations
```json
{
  "task_id": "calculator_001",
  "description": "Perform compound interest calculation",
  "required_tools": ["Calculator:calculate_compound_interest"],
  "type": "computation"
}
```

### Tool Catalog

**Core Servers (5):**
| Server | Tools | Description |
|--------|-------|-------------|
| OpenAPI Explorer | 5 | API introspection |
| Calculator | 6 | Mathematical operations |
| File System | 8 | File operations |
| Database | 6 | SQL queries |
| Web Search | 5 | Internet search |

**Distraction Servers:**
- 5 distractions: 30 additional irrelevant tools (35 total)
- 15 distractions: 60 additional irrelevant tools (65 total)

Distraction tools are semantically similar but unrelated to task requirements (e.g., "Statistics:compute_variance" when the task needs "Calculator:calculate_compound_interest").

## Approach Implementations

### Static Loading

**Implementation:** `agent/executor.py`

```python
class StaticExecutor:
    def __init__(self, server_manager):
        # Load ALL tools at initialization
        self.all_tools = server_manager.all_tools  # 35-65 tools

    def build_prompt(self, task):
        # Every prompt includes complete tool catalog
        return f"""
        AVAILABLE TOOLS:
        {self.format_all_tools()}

        TASK: {task}
        """
```

**Characteristics:**
- Full tool visibility from round 1
- Direct tool invocation
- Token usage scales linearly with tool count

### Dynamic Discovery

**Implementation:** `agent/dynamic_executor.py`, `agent/dynamic_discovery.py`

```python
# Discovery tools always in context
DISCOVERY_TOOLS = {
    "nexus_discovery:search_tools": {
        "description": "Search for tools by query",
        "input_schema": {"query": "string"}
    },
    "nexus_discovery:list_servers": {
        "description": "List all available servers"
    },
    "nexus_discovery:get_tool_details": {
        "description": "Get detailed info about a tool",
        "input_schema": {"tool_name": "string"}
    },
    "nexus_discovery:load_tools": {
        "description": "Load tools into active context",
        "input_schema": {"tool_names": "array"}
    }
}

class DynamicDiscovery:
    def __init__(self, all_tools):
        self.all_tools = all_tools
        self.active_tools = set()  # Only discovery tools initially

    def search_tools(self, query: str) -> list:
        # Returns tools with relevance scores
        # Agent sees these results and decides what to load
        return [
            {"name": "Calculator:calculate_compound_interest", "score": 0.92},
            {"name": "Calculator:calculate_simple_interest", "score": 0.85},
        ]

    def load_tools(self, tool_names: list):
        # Adds tools to active context for subsequent rounds
        self.active_tools.update(tool_names)
```

**Agent Workflow:**
1. Round 1: Search for relevant tools
2. Round 2: Load discovered tools
3. Round 3+: Use loaded tools

**Characteristics:**
- Minimal initial context (4 discovery tools)
- Agent-controlled tool loading
- Can refine searches based on results

### Anthropic Tool Search

**Implementation:** `agent/anthropic_tool_search_executor.py`

```python
ADVANCED_TOOL_USE_BETA = "advanced-tool-use-2025-11-20"

class AnthropicToolSearchExecutor:
    def __init__(self, all_tools):
        # Client with beta header
        self.client = anthropic.AsyncAnthropic(
            default_headers={"anthropic-beta": ADVANCED_TOOL_USE_BETA}
        )

        # Convert tools with defer_loading
        self.tools = self._convert_tools(all_tools)

    def _convert_tools(self, all_tools):
        tools = []
        for i, (name, info) in enumerate(all_tools.items()):
            # Sanitize name: colons/spaces not allowed
            sanitized = name.replace(":", "_").replace(" ", "_")

            tool_def = {
                "name": sanitized,
                "description": f"[{name}] {info['description']}",
                "input_schema": info['input_schema'],
                # First tool non-deferred (API requirement)
                "defer_loading": i > 0
            }
            tools.append(tool_def)
        return tools
```

**How It Works:**
1. 64 of 65 tools have `defer_loading: true`
2. Only tool names/descriptions sent in context
3. When model requests a tool, Anthropic's backend searches using BM25
4. Best match returned and executed

**Characteristics:**
- Minimal context (names/descriptions only)
- Server-side BM25 search
- No visibility into search results
- No ability to refine queries

## Metrics

### Token Usage
- Measured using OpenAI tokenizer (tiktoken)
- Includes system prompt, user messages, assistant responses, tool calls

### Tool Call Success Rate
- `successful_calls / total_calls`
- A call is successful if:
  - Tool name resolves correctly
  - Parameters match schema
  - No execution error

### Task Completion Score (1-10)
- Evaluated by Claude Opus 4.5 judge
- Criteria:
  - Did the agent complete the task?
  - Was the output accurate?
  - Was the approach efficient?

### Planning Score (1-10)
- How well did the agent plan tool usage?
- Appropriate tool sequence?
- Efficient dependency handling?

## Experimental Procedure

```bash
# Run benchmark for each configuration
for APPROACH in static dynamic anthropic_tool_search; do
    for DISTRACTIONS in 5 15; do
        python benchmark/runner.py \
            --approach $APPROACH \
            --distraction-count $DISTRACTIONS \
            --tasks-file ./tasks/test_2tasks.json \
            --output ./results/${APPROACH}_${DISTRACTIONS}.json
    done
done
```

Each configuration:
1. Initialize server manager and connect to MCP servers
2. Load tool catalog (core + distractions)
3. For each task:
   - Execute agent loop (max 20 rounds)
   - Record all metrics
   - Evaluate with judge model
4. Aggregate results

## Reproducibility

### Random Seeds
- Python random seed: 42
- No stochastic elements in benchmark logic

### Caching
- Tool cache enabled with permanent TTL
- Cache directory: `./cache`
- Ensures consistent tool responses across runs

### Environment Variables
```bash
ANTHROPIC_API_KEY=<your-key>
```

## Limitations

1. **Small task set**: Only 2 tasks used due to cost constraints
2. **Single model family**: All agents use Claude models
3. **Synthetic distractions**: Distraction tools are programmatically generated
4. **No concurrent execution**: All runs sequential

## Data Availability

Raw results available in:
- `results/benchmark_data.json` - Aggregated metrics
- Individual run files in `benchmark_results_*/` directories

## Citation

If using this benchmark, please cite:

```bibtex
@misc{nexus_discovery_benchmark_2025,
  title={MCP Tool Discovery Benchmark: Comparing Static, Dynamic, and Anthropic Tool Search},
  author={Nexus Team},
  year={2025},
  month={November},
  url={https://github.com/nexi-lab/nexus}
}
```
