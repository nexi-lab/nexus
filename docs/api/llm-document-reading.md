# LLM Document Reading API

LLM-powered document reading allows you to ask questions about documents and get AI-powered answers with citations and cost tracking.

## Overview

The LLM Document Reading feature combines:
- **Semantic Search**: Intelligent context retrieval (optional)
- **LLM Processing**: AI-powered question answering
- **Citation Extraction**: Automatic source attribution
- **Cost Tracking**: Token usage and cost monitoring

## Quick Start

### Python SDK

```python
from nexus import connect

async def main():
    nx = connect()

    # Simple usage - just get the answer
    answer = await nx.llm_read(
        path="/reports/q4.pdf",
        prompt="What were the top 3 challenges?",
        model="claude-sonnet-4"
    )
    print(answer)
```

### CLI

```bash
# Ask a question about a document
nexus llm read /reports/q4.pdf "What were the top 3 challenges?"

# Query multiple documents
nexus llm read "/docs/**/*.md" "How does authentication work?"

# Get detailed output with citations
nexus llm read /docs/api.md "List endpoints" --detailed
```

## Python API

### Basic Methods

#### `llm_read()`

Simple method that returns just the answer text.

```python
async def llm_read(
    path: str,
    prompt: str,
    model: str = "claude-sonnet-4",
    max_tokens: int = 1000,
    api_key: str | None = None,
    use_search: bool = True,
    search_mode: str = "semantic",
    provider: LLMProvider | None = None,
) -> str
```

**Parameters:**
- `path`: Document path or glob pattern (e.g., `/report.pdf` or `/docs/**/*.md`)
- `prompt`: Your question or instruction
- `model`: LLM model to use (default: `claude-sonnet-4`)
- `max_tokens`: Maximum tokens in response (default: 1000)
- `api_key`: API key for LLM provider (optional, reads from environment)
- `use_search`: Use semantic search for context retrieval (default: True)
- `search_mode`: Search mode - `"semantic"`, `"keyword"`, or `"hybrid"` (default: `"semantic"`)
- `provider`: Pre-configured LLM provider (optional)

**Returns:** String answer from the LLM

**Example:**
```python
answer = await nx.llm_read(
    "/reports/q4.pdf",
    "What were the key achievements?",
    model="claude-sonnet-4",
    max_tokens=500
)
print(answer)
```

#### `llm_read_detailed()`

Returns full result with answer, citations, sources, tokens, and cost.

```python
async def llm_read_detailed(
    path: str,
    prompt: str,
    model: str = "claude-sonnet-4",
    max_tokens: int = 1000,
    api_key: str | None = None,
    use_search: bool = True,
    search_mode: str = "semantic",
    search_limit: int = 10,
    include_citations: bool = True,
    provider: LLMProvider | None = None,
) -> DocumentReadResult
```

**Parameters:** Same as `llm_read()` plus:
- `search_limit`: Maximum search results to use (default: 10)
- `include_citations`: Extract and include citations (default: True)

**Returns:** `DocumentReadResult` object with:
- `answer`: LLM's answer text
- `citations`: List of `Citation` objects with source references
- `sources`: List of source file paths
- `tokens_used`: Total tokens used
- `cost`: Cost in USD
- `cached`: Whether response was cached

**Example:**
```python
result = await nx.llm_read_detailed(
    "/docs/**/*.md",
    "How does authentication work?",
    model="claude-sonnet-4"
)

print(result.answer)
print(f"\nSources ({len(result.citations)}):")
for citation in result.citations:
    print(f"  • {citation.path} (score: {citation.score:.2f})")
print(f"\nCost: ${result.cost:.4f}")
print(f"Tokens: {result.tokens_used:,}")
```

#### `llm_read_stream()`

Stream the response for real-time output.

```python
async def llm_read_stream(
    path: str,
    prompt: str,
    model: str = "claude-sonnet-4",
    max_tokens: int = 1000,
    api_key: str | None = None,
    use_search: bool = True,
    search_mode: str = "semantic",
    provider: LLMProvider | None = None,
) -> AsyncIterator[str]
```

**Yields:** Response chunks as strings

**Example:**
```python
async for chunk in nx.llm_read_stream(
    "/report.pdf",
    "Analyze the financial trends",
    model="claude-sonnet-4"
):
    print(chunk, end="", flush=True)
print()  # Newline after streaming
```

#### `create_llm_reader()`

Create a reader instance for advanced usage with custom configuration.

```python
def create_llm_reader(
    provider: LLMProvider | None = None,
    model: str | None = None,
    api_key: str | None = None,
    system_prompt: str | None = None,
    max_context_tokens: int = 3000,
) -> LLMDocumentReader
```

**Parameters:**
- `provider`: Pre-configured LLM provider instance
- `model`: Model name (default: `claude-sonnet-4`)
- `api_key`: API key for provider
- `system_prompt`: Custom system prompt for the LLM
- `max_context_tokens`: Maximum tokens for context (default: 3000)

**Returns:** `LLMDocumentReader` instance

**Example:**
```python
# Create custom reader
reader = nx.create_llm_reader(
    model="claude-opus-4",
    system_prompt=(
        "You are a technical documentation expert. "
        "Provide detailed, precise answers with code examples when relevant."
    )
)

# Use the reader
result = await reader.read(
    path="/docs/**/*.md",
    prompt="Explain the architecture",
    max_tokens=1000
)
print(result.answer)
```

## CLI Reference

### `nexus llm read`

Read and analyze documents with LLM.

```bash
nexus llm read PATH PROMPT [OPTIONS]
```

**Arguments:**
- `PATH`: Document path or glob pattern (e.g., `/report.pdf` or `/docs/**/*.md`)
- `PROMPT`: Your question or instruction

**Options:**
- `--model TEXT`: LLM model to use (default: `claude-sonnet-4`)
  - Anthropic: `claude-sonnet-4`, `claude-opus-4`, `claude-haiku-4`
  - OpenAI: `gpt-4o`, `gpt-4o-mini`
  - OpenRouter: `anthropic/claude-sonnet-4.5`, `openrouter/google/gemini-pro-1.5`
- `--max-tokens INTEGER`: Maximum tokens in response (default: 1000)
- `--api-key TEXT`: API key for LLM provider (or set env var)
- `--no-search`: Disable semantic search (read entire document)
- `--search-mode [semantic|keyword|hybrid]`: Search mode for context retrieval (default: `semantic`)
- `--stream`: Stream the response (show output as it's generated)
- `--detailed`: Show detailed output with citations and metadata
- `--remote-url TEXT`: Remote Nexus server URL (or set `NEXUS_URL`)
- `--remote-api-key TEXT`: Remote server API key (or set `NEXUS_API_KEY`)

**Examples:**

```bash
# Basic question answering
nexus llm read /reports/q4.pdf "What were the top 3 challenges?"

# Query multiple documents
nexus llm read "/docs/**/*.md" "How does authentication work?"

# Use different model
nexus llm read /report.pdf "Summarize this" --model gpt-4o

# Stream response
nexus llm read /long-report.pdf "Analyze trends" --stream

# Get detailed output with citations
nexus llm read /docs/**/*.md "Explain the API" --detailed

# Disable semantic search (read full document)
nexus llm read /report.txt "Summarize" --no-search

# Use keyword search instead of semantic
nexus llm read /docs/**/*.md "API endpoints" --search-mode keyword

# Use with remote server
nexus llm read /doc.pdf "Question" --remote-url http://localhost:8080
```

## Supported Models

### Anthropic Claude

Set `ANTHROPIC_API_KEY` environment variable or use `--api-key` flag.

```bash
export ANTHROPIC_API_KEY="sk-ant-..."

# Use Claude models
nexus llm read /doc.pdf "Question" --model claude-sonnet-4
nexus llm read /doc.pdf "Question" --model claude-opus-4
nexus llm read /doc.pdf "Question" --model claude-haiku-4
```

Available models:
- `claude-sonnet-4` - Balanced performance and cost (recommended)
- `claude-opus-4` - Highest capability for complex analysis
- `claude-haiku-4` - Fastest and most cost-effective

### OpenAI GPT

Set `OPENAI_API_KEY` environment variable or use `--api-key` flag.

```bash
export OPENAI_API_KEY="sk-..."

# Use GPT models
nexus llm read /doc.pdf "Question" --model gpt-4o
nexus llm read /doc.pdf "Question" --model gpt-4o-mini
```

Available models:
- `gpt-4o` - Latest GPT-4 Optimized model
- `gpt-4o-mini` - Faster, more cost-effective

### OpenRouter (100+ Models)

Set `OPENROUTER_API_KEY` environment variable. Provides access to 100+ models from multiple providers.

```bash
export OPENROUTER_API_KEY="sk-or-..."

# Use any model via OpenRouter
nexus llm read /doc.pdf "Question" --model anthropic/claude-sonnet-4.5
nexus llm read /doc.pdf "Question" --model anthropic/claude-haiku-4.5
nexus llm read /doc.pdf "Question" --model openrouter/google/gemini-pro-1.5
```

Popular OpenRouter models:
- `anthropic/claude-sonnet-4.5` - Latest Claude (recommended)
- `anthropic/claude-haiku-4.5` - Fast Claude
- `anthropic/claude-opus-4` - Most capable Claude
- `openrouter/openai/gpt-4o`
- `openrouter/google/gemini-pro-1.5`

Get your API key: https://openrouter.ai/keys

See all available models: https://openrouter.ai/models

## Search Modes

### Semantic Search (Default)

Uses vector embeddings for intelligent context retrieval. Best for conceptual questions.

```python
# Python
answer = await nx.llm_read(
    "/docs/**/*.md",
    "How does authentication work?",
    search_mode="semantic"  # Default
)

# CLI
nexus llm read "/docs/**/*.md" "authentication" --search-mode semantic
```

**Best for:**
- Conceptual questions
- Summarization
- Understanding relationships
- Finding relevant context across documents

### Keyword Search

Uses traditional full-text search (FTS). Best for exact terms and code.

```python
# Python
answer = await nx.llm_read(
    "/src/**/*.py",
    "Find JWT token implementation",
    search_mode="keyword"
)

# CLI
nexus llm read "/src/**/*.py" "JWT token" --search-mode keyword
```

**Best for:**
- Exact terms and phrases
- Code snippets
- Function/class names
- Technical identifiers

### Hybrid Search

Combines both semantic and keyword search for comprehensive results.

```python
# Python
answer = await nx.llm_read(
    "/docs/**/*.md",
    "API security best practices",
    search_mode="hybrid"
)

# CLI
nexus llm read "/docs/**/*.md" "API security" --search-mode hybrid
```

**Best for:**
- Complex queries
- When you need both conceptual understanding and specific details
- Large document collections

### No Search

Reads entire document(s) without search. Best for small files or when you want complete context.

```python
# Python
answer = await nx.llm_read(
    "/config.yaml",
    "Explain this configuration",
    use_search=False
)

# CLI
nexus llm read /config.yaml "Explain this" --no-search
```

**Best for:**
- Small files (<4000 tokens)
- Complete document analysis
- When semantic search is not available

## Data Models

### DocumentReadResult

Returned by `llm_read_detailed()`.

```python
@dataclass
class DocumentReadResult:
    answer: str                      # LLM's answer
    citations: list[Citation]        # Source citations
    sources: list[str]               # Source file paths
    tokens_used: int | None          # Total tokens used
    cost: float | None               # Cost in USD
    cached: bool                     # Whether cached
    cache_savings: float | None      # Cache savings in USD
```

### Citation

Source reference with optional relevance score.

```python
@dataclass
class Citation:
    path: str                        # Source file path
    chunk_index: int | None          # Chunk index in file
    score: float | None              # Relevance score (0-1)
    start_offset: int | None         # Start position in file
    end_offset: int | None           # End position in file
```

## Advanced Usage

### Custom System Prompt

Customize the LLM's behavior with a custom system prompt.

```python
# Create reader with custom prompt
reader = nx.create_llm_reader(
    model="claude-sonnet-4",
    system_prompt=(
        "You are an executive assistant. Provide concise, "
        "bullet-point summaries focused on key business metrics and "
        "actionable insights. Use executive language and avoid technical jargon."
    )
)

# Use the customized reader
result = await reader.read(
    path="/reports/q4-2024.txt",
    prompt="Summarize Q4 performance for the executive team",
    max_tokens=400
)
print(result.answer)
```

### Pre-configured Provider

Use a pre-configured LLM provider for advanced control.

```python
from nexus.llm import LiteLLMProvider, LLMConfig

# Configure provider
config = LLMConfig(
    model="claude-sonnet-4",
    api_key="your-api-key",
    temperature=0.7,
    max_output_tokens=2000,
    timeout=60
)
provider = LiteLLMProvider(config)

# Use with LLM reading
answer = await nx.llm_read(
    "/docs/api.md",
    "What endpoints are available?",
    provider=provider
)
```

### Cost Management

Track costs and optimize spending.

```python
# Get detailed result with cost
result = await nx.llm_read_detailed(
    "/docs/**/*.md",
    "Comprehensive analysis",
    model="claude-sonnet-4"
)

print(f"Cost: ${result.cost:.4f}")
print(f"Tokens: {result.tokens_used:,}")

# Use cheaper model for simple queries
answer = await nx.llm_read(
    "/doc.txt",
    "Simple question",
    model="claude-haiku-4",  # Cheaper
    max_tokens=200           # Limit response
)

# Use keyword search to avoid embedding costs
answer = await nx.llm_read(
    "/code/**/*.py",
    "Find function definition",
    search_mode="keyword"  # No embedding costs
)
```

### Multi-Language Support

LLMs support multiple languages automatically.

```python
# Ask in Chinese
answer = await nx.llm_read(
    "/doc.pdf",
    "请用中文总结这个文档",
    model="claude-sonnet-4"
)

# Ask in Spanish
answer = await nx.llm_read(
    "/report.pdf",
    "Resume este informe en español",
    model="claude-sonnet-4"
)
```

### Remote Server Mode

Use with remote Nexus server.

```python
import os

# Set remote server credentials
os.environ["NEXUS_URL"] = "http://localhost:8080"
os.environ["NEXUS_API_KEY"] = "your-api-key"

# Connect to remote server
nx = connect()

# LLM reading works exactly the same
answer = await nx.llm_read(
    "/workspace/doc.pdf",
    "What are the key points?",
    model="claude-sonnet-4"
)
```

## Error Handling

```python
from nexus.core.exceptions import (
    NexusFileNotFoundError,
    NexusPermissionError,
    ValidationError
)

try:
    answer = await nx.llm_read(
        "/reports/q4.pdf",
        "What were the challenges?",
        model="claude-sonnet-4"
    )
    print(answer)
except NexusFileNotFoundError:
    print("Document not found")
except NexusPermissionError:
    print("Permission denied")
except ValidationError as e:
    print(f"Invalid input: {e}")
except Exception as e:
    print(f"Error: {e}")
```

## Integration with Semantic Search

For best results, index documents first:

```python
# Index documents for semantic search
nx.search_index("/workspace/docs")

# Now LLM reading will use semantic search automatically
result = await nx.llm_read_detailed(
    "/workspace/docs/**/*.md",
    "How does the system work?",
    model="claude-sonnet-4"
)
```

Without indexing, LLM reading will fall back to direct file reading.

See [Semantic Search API](semantic-search.md) for indexing details.

## Complete Example

```python
import asyncio
from nexus import connect

async def analyze_documentation():
    """Analyze project documentation with LLM."""
    nx = connect()

    # Index documents for better search
    print("Indexing documents...")
    nx.search_index("/workspace/docs")

    # Ask questions
    questions = [
        "What are the main features?",
        "How do I get started?",
        "What are the API endpoints?",
    ]

    for question in questions:
        print(f"\nQ: {question}")

        result = await nx.llm_read_detailed(
            path="/workspace/docs/**/*.md",
            prompt=question,
            model="claude-sonnet-4",
            max_tokens=500
        )

        print(f"A: {result.answer}")
        print(f"\nSources: {len(result.sources)}")
        print(f"Cost: ${result.cost:.4f}")
        print("-" * 60)

if __name__ == "__main__":
    asyncio.run(analyze_documentation())
```

## Performance Tips

1. **Index documents**: For large document collections, index with `search_index()` first
2. **Use appropriate models**:
   - `claude-haiku-4` for simple queries (fast, cheap)
   - `claude-sonnet-4` for balanced performance (recommended)
   - `claude-opus-4` for complex analysis (most capable)
3. **Limit tokens**: Use `max_tokens` to control response length and cost
4. **Choose search mode**:
   - `semantic` for conceptual questions
   - `keyword` for exact terms (no embedding cost)
   - `hybrid` for comprehensive results
5. **Use `--no-search`**: For small files, reading directly is faster
6. **Batch queries**: Group related questions to reuse context
7. **Cache embeddings**: Semantic search caches embeddings for reuse

## See Also

- [Semantic Search API](semantic-search.md) - Vector search and indexing
- [LLM Provider Integration](../integrations/llm.md) - LLM provider setup
- [CLI Reference](cli-reference.md) - Complete CLI documentation
- [Examples](../../examples/LLM_QUICK_START.md) - More examples and demos

---

**Related Examples:**
- `examples/py_demo/llm_document_reading_demo.py` - Python demo
- `examples/cli/llm_document_reading_demo.sh` - CLI demo
- `examples/LLM_QUICK_START.md` - Quick start guide
