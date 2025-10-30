# LLM Document Reading - CLI Reference

← [CLI Reference](index.md)

AI-powered document question answering with citations and cost tracking.

## Overview

The LLM document reading commands enable you to ask questions about your documents and get AI-powered answers with automatic citation extraction and cost tracking. The feature integrates seamlessly with semantic search for intelligent context retrieval.

**Key Features:**
- Ask natural language questions about documents
- Get answers with citations and source attribution
- Track tokens used and costs
- Support for multiple LLM providers (Claude, GPT, OpenRouter)
- Multiple search modes (semantic, keyword, hybrid)
- Streaming responses for real-time output
- Works with both local and remote Nexus servers

## Commands

### llm read - Ask questions about documents

Ask natural language questions about documents and get AI-powered answers.

**Syntax:**
```bash
nexus llm read PATH PROMPT [OPTIONS]
```

**Arguments:**
- `PATH` - Document path or glob pattern (e.g., `/report.pdf` or `/docs/**/*.md`)
- `PROMPT` - Your question or instruction

**Options:**
- `--model TEXT` - LLM model to use (default: `claude-sonnet-4`)
- `--max-tokens INTEGER` - Maximum tokens in response (default: 1000)
- `--api-key TEXT` - API key for LLM provider (or set environment variable)
- `--no-search` - Disable semantic search (read entire document)
- `--search-mode [semantic|keyword|hybrid]` - Search mode for context retrieval (default: `semantic`)
- `--stream` - Stream the response (show output as it's generated)
- `--detailed` - Show detailed output with citations and metadata
- `--remote-url URL` - Remote Nexus server URL (or use `NEXUS_URL` env var)
- `--remote-api-key KEY` - Remote server API key (or use `NEXUS_API_KEY` env var)

**Environment Variables:**
- `ANTHROPIC_API_KEY` - Anthropic Claude API key
- `OPENAI_API_KEY` - OpenAI API key
- `OPENROUTER_API_KEY` - OpenRouter API key
- `NEXUS_URL` - Remote Nexus server URL
- `NEXUS_API_KEY` - Remote Nexus server API key

## Examples

### Basic Usage

**Simple question answering:**
```bash
nexus llm read /reports/q4.pdf "What were the top 3 challenges?"
```

**Query multiple documents:**
```bash
nexus llm read "/docs/**/*.md" "How does authentication work?"
```

**Different models:**
```bash
# Use GPT-4
nexus llm read /report.pdf "Summarize this" --model gpt-4o

# Use Claude Opus (most capable)
nexus llm read /report.pdf "Complex analysis" --model claude-opus-4

# Use OpenRouter
nexus llm read /report.pdf "Question" --model anthropic/claude-sonnet-4.5
```

### Advanced Usage

**Stream response for real-time output:**
```bash
nexus llm read /long-report.pdf "Analyze financial trends" --stream
```

**Get detailed output with citations:**
```bash
nexus llm read /docs/**/*.md "Explain the API" --detailed
```

Output:
```
Answer:
The API provides RESTful endpoints for user management and file operations...

Sources (2):
  • /docs/api.md (relevance: 0.89)
  • /docs/auth.md (relevance: 0.82)

Citations (2):
  1. /docs/api.md
  2. /docs/auth.md

Metadata:
  Tokens: 1,234
  Cost: $0.0045
```

**Disable semantic search (read full document):**
```bash
nexus llm read /config.yaml "Explain this configuration" --no-search
```

**Use keyword search instead of semantic:**
```bash
nexus llm read /docs/**/*.md "JWT token implementation" --search-mode keyword
```

**Use hybrid search (semantic + keyword):**
```bash
nexus llm read /docs/**/*.md "API security best practices" --search-mode hybrid
```

### Use Cases

**Document Q&A:**
```bash
nexus llm read /manual.pdf "How do I configure SSL certificates?"
```

**Code documentation:**
```bash
nexus llm read "/src/**/*.py" "What design patterns are used?"
```

**Research analysis:**
```bash
nexus llm read "/papers/**/*.pdf" "Compare the methodologies" --detailed
```

**Report summarization:**
```bash
nexus llm read "/reports/*.txt" "Summarize key metrics and trends" --stream
```

**Multi-language support:**
```bash
# Ask in Chinese
nexus llm read /doc.pdf "请用中文总结这个文档" --model claude-sonnet-4

# Ask in Spanish
nexus llm read /report.pdf "Resume este informe en español"
```

## Supported Models

### Anthropic Claude

**Setup:**
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

**Models:**
- `claude-sonnet-4` - Balanced performance and cost (**recommended**)
- `claude-opus-4` - Most capable for complex analysis
- `claude-haiku-4` - Fastest and most cost-effective

**Example:**
```bash
nexus llm read /doc.pdf "Question" --model claude-sonnet-4
```

### OpenAI GPT

**Setup:**
```bash
export OPENAI_API_KEY="sk-..."
```

**Models:**
- `gpt-4o` - Latest GPT-4 Optimized model
- `gpt-4o-mini` - Faster and more cost-effective

**Example:**
```bash
nexus llm read /doc.pdf "Question" --model gpt-4o
```

### OpenRouter (100+ Models)

OpenRouter provides access to 100+ models from multiple providers.

**Setup:**
```bash
export OPENROUTER_API_KEY="sk-or-..."
```

**Popular Models:**
- `anthropic/claude-sonnet-4.5` - Latest Claude (**recommended**)
- `anthropic/claude-haiku-4.5` - Fast Claude
- `anthropic/claude-opus-4` - Most capable Claude
- `openrouter/openai/gpt-4o` - GPT-4 via OpenRouter
- `openrouter/google/gemini-pro-1.5` - Google Gemini

**Get API key:** https://openrouter.ai/keys

**See all models:** https://openrouter.ai/models

**Example:**
```bash
nexus llm read /doc.pdf "Question" --model anthropic/claude-sonnet-4.5
```

## Search Modes

### Semantic Search (Default)

Uses vector embeddings for intelligent context retrieval. Best for conceptual questions.

```bash
nexus llm read /docs/**/*.md "How does authentication work?"
# or explicitly
nexus llm read /docs/**/*.md "authentication" --search-mode semantic
```

**Best for:**
- Conceptual questions
- Summarization
- Understanding relationships
- Finding relevant context across documents

**Requires:** Semantic search must be initialized with `nexus search init`

### Keyword Search

Uses traditional full-text search (FTS). Best for exact terms and code.

```bash
nexus llm read /src/**/*.py "JWT token implementation" --search-mode keyword
```

**Best for:**
- Exact terms and phrases
- Code snippets
- Function/class names
- Technical identifiers

**No setup required**

### Hybrid Search

Combines both semantic and keyword search for comprehensive results.

```bash
nexus llm read /docs/**/*.md "API security best practices" --search-mode hybrid
```

**Best for:**
- Complex queries
- When you need both conceptual understanding and specific details
- Large document collections

### No Search

Reads entire document(s) without search. Best for small files.

```bash
nexus llm read /config.yaml "Explain this configuration" --no-search
```

**Best for:**
- Small files (<4000 tokens)
- Complete document analysis
- When semantic search is not available

## Remote Server Mode

LLM document reading works seamlessly with remote Nexus servers.

**Setup:**
```bash
# Start server
nexus serve --host 0.0.0.0 --port 8080 --api-key secret123

# In another terminal, set environment
export NEXUS_URL=http://localhost:8080
export NEXUS_API_KEY=secret123
export ANTHROPIC_API_KEY=sk-ant-...
```

**Use normally:**
```bash
nexus llm read /workspace/doc.pdf "What are the key points?"
```

**Or use flags:**
```bash
nexus llm read /workspace/doc.pdf "Question" \
  --remote-url http://localhost:8080 \
  --remote-api-key secret123
```

## Cost Management

### View Costs

Use `--detailed` to see token usage and costs:

```bash
nexus llm read /doc.pdf "Question" --detailed
```

Output includes:
```
Metadata:
  Tokens: 1,234
  Cost: $0.0045
```

### Reduce Costs

**Use cheaper models:**
```bash
# Haiku is 5-10x cheaper than Sonnet
nexus llm read /doc.pdf "Simple question" --model claude-haiku-4
```

**Limit response tokens:**
```bash
nexus llm read /doc.pdf "Brief summary" --max-tokens 200
```

**Use keyword search (no embedding costs):**
```bash
nexus llm read /code/**/*.py "function name" --search-mode keyword
```

**Read small files directly:**
```bash
nexus llm read /config.yaml "Explain" --no-search
```

## Performance Tips

1. **Index documents first** - For large collections, run `nexus search index` before querying
2. **Use appropriate models:**
   - `claude-haiku-4` for simple queries (fast, cheap)
   - `claude-sonnet-4` for balanced performance (recommended)
   - `claude-opus-4` for complex analysis (most capable)
3. **Choose the right search mode:**
   - `semantic` for conceptual questions
   - `keyword` for exact terms (faster, no embedding cost)
   - `hybrid` for comprehensive results
   - `--no-search` for small files (fastest)
4. **Limit tokens** - Use `--max-tokens` to control response length
5. **Stream long responses** - Use `--stream` for better perceived performance

## Common Workflows

### 1. Quick document Q&A

```bash
# Set API key once
export ANTHROPIC_API_KEY=sk-ant-...

# Ask questions
nexus llm read /reports/q4.pdf "What were the challenges?"
nexus llm read /reports/q4.pdf "What are the action items?"
```

### 2. Research multiple documents

```bash
# Index documents first
nexus search index /papers

# Query with semantic search
nexus llm read "/papers/**/*.pdf" "Compare the methodologies" --detailed
```

### 3. Code documentation

```bash
# Use keyword search for code
nexus llm read "/src/**/*.py" "Find authentication implementation" \
  --search-mode keyword
```

### 4. Report generation

```bash
# Stream long analysis
nexus llm read "/data/**/*.csv" "Analyze trends and create executive summary" \
  --stream --max-tokens 2000 --model claude-sonnet-4
```

### 5. Multi-tenant analysis (with remote server)

```bash
# Setup
export NEXUS_URL=http://nexus.company.com
export NEXUS_API_KEY=user_alice_key
export NEXUS_TENANT_ID=org_acme
export ANTHROPIC_API_KEY=sk-ant-...

# Alice queries her documents
nexus llm read "/workspace/alice/**/*.md" "Summarize my project docs"
```

## Error Handling

### Common Errors

**"No LLM API key found"**
```bash
# Set one of these:
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
export OPENROUTER_API_KEY=sk-or-...
```

**"Semantic search requires embedding provider"**
```bash
# Option 1: Initialize semantic search
nexus search init --provider openai

# Option 2: Use keyword search
nexus llm read /doc.pdf "Question" --search-mode keyword

# Option 3: Disable search for small files
nexus llm read /doc.pdf "Question" --no-search
```

**"File not found"**
```bash
# Use absolute paths or check current directory
nexus ls /workspace
nexus llm read /workspace/doc.pdf "Question"
```

**"Permission denied"**
```bash
# Check permissions
nexus rebac check user alice read file /workspace/doc.pdf

# Grant read permission
nexus rebac create user alice direct_viewer file /workspace/doc.pdf
```

## Python API Equivalent

Every CLI command has a Python API equivalent:

```bash
# CLI
nexus llm read /doc.pdf "Question" --model claude-sonnet-4 --detailed
```

```python
# Python
import asyncio
from nexus import connect

async def main():
    nx = connect()
    result = await nx.llm_read_detailed(
        path="/doc.pdf",
        prompt="Question",
        model="claude-sonnet-4"
    )
    print(result.answer)
    print(f"Cost: ${result.cost:.4f}")

asyncio.run(main())
```

See [LLM Document Reading API](../llm-document-reading.md) for complete Python SDK documentation.

## See Also

- [LLM Document Reading API](../llm-document-reading.md) - Python SDK reference
- [Semantic Search CLI](semantic-search.md) - Document indexing
- [Search Operations CLI](search.md) - File search commands
- [Examples Quick Start](../../../examples/LLM_QUICK_START.md) - Getting started guide
- **Demo Scripts:**
  - `examples/cli/llm_document_reading_demo.sh` - CLI demo
  - `examples/py_demo/llm_document_reading_demo.py` - Python demo
