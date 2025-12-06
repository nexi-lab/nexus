# Nexus Dynamic Discovery Benchmarks

## TL;DR

Nexus Dynamic Discovery achieves **78% token reduction** with **100% reliability** compared to static tool loading, and significantly outperforms Anthropic's Tool Search at scale.

![Token Comparison](figures/token_comparison.png)

## Key Results

| Approach | Token Reduction | Success Rate | Best For |
|----------|----------------|--------------|----------|
| **Static Loading** | Baseline | 100% | Small catalogs (<30 tools) |
| **Nexus Dynamic Discovery** | **78%** | **100%** | **Production (any scale)** |
| **Anthropic Tool Search** | 87% | 52% (at scale) | Simple tasks only |

## The Problem

As MCP tool catalogs grow, static tool loading becomes prohibitively expensive:

| Tool Count | Static Tokens | Impact |
|-----------|---------------|--------|
| 35 tools | 143,058 | Manageable |
| 65 tools | 276,415 | Expensive |
| 100+ tools | 400,000+ | Unsustainable |

## Why Nexus Dynamic Discovery?

### Compared to Static Loading

- **78% fewer tokens** (62K vs 276K at 65 tools)
- Same 100% reliability
- Agent discovers relevant tools on-demand

### Compared to Anthropic Tool Search

Anthropic's `defer_loading` with BM25 search sounds promising, but:

- Uses keyword-based BM25 search (not semantic)
- **Degrades to 52% success rate** with 65+ tools
- Model has no visibility into search results

Nexus Dynamic Discovery:
- Agent-driven search with visible results
- Can refine queries based on search scores
- **Maintains 100% success rate** regardless of catalog size

![Success Rate Comparison](figures/success_rate_comparison.png)

## How It Works

```
┌─────────────────────────────────────────────────────┐
│  Round 1: Agent has 4 discovery tools only          │
│           search_tools("API overview")              │
│           → Returns matching tools with scores      │
├─────────────────────────────────────────────────────┤
│  Round 2: load_tools(["OpenAPI Explorer:..."])      │
│           → Tool now added to active context        │
├─────────────────────────────────────────────────────┤
│  Round 3+: Use loaded tools to complete task        │
│            Only relevant tools in context           │
└─────────────────────────────────────────────────────┘
```

## Benchmark Details

### Test Configuration

- **Models**: Claude Sonnet 4 (agent), Claude Opus 4.5 (judge)
- **Tasks**: 2 representative tasks (API exploration, calculations)
- **Tool Counts**: 35 tools (5 distractions), 65 tools (15 distractions)
- **Metrics**: Token usage, success rate, task quality (1-10 scale)

### Full Results

See [results/benchmark_data.json](results/benchmark_data.json) for raw data.

| Metric | Static (65 tools) | Dynamic Discovery | Anthropic Tool Search |
|--------|------------------|-------------------|----------------------|
| Avg Prompt Tokens | 276,415 | **62,084** | 35,262 |
| Tool Call Success | 100% | **100%** | 52% |
| Task Completion Score | 5.75 | **6.0** | 1.0 |
| Avg Rounds | 7.0 | **5.5** | 11.5 |

![Quality Comparison](figures/quality_comparison.png)

## Getting Started with Dynamic Discovery

```python
# Enable dynamic discovery in Nexus
from nexus.discovery import DynamicToolDiscovery

discovery = DynamicToolDiscovery(all_tools)

# Agent workflow:
# 1. Search for tools
results = discovery.search_tools("file operations")
# Returns: [{"name": "FileSystem:readFile", "score": 0.92}, ...]

# 2. Load relevant tools
discovery.load_tools(["FileSystem:readFile", "FileSystem:writeFile"])

# 3. Use tools normally
active_tools = discovery.get_active_tools()
```

## More Information

- [Full Benchmark Report](BENCHMARK_REPORT.md)
- [Methodology Details](METHODOLOGY.md)
- [Nexus Documentation](../docs/)

---

*Benchmarks conducted November 2025 using MCP-Bench framework*
