# LLM Document Reading - Quick Start Guide

This guide shows you how to use Nexus's LLM-powered document reading feature.

## Prerequisites

### 1. Install Nexus
```bash
pip install nexus-ai-fs
```

### 2. Set API Key

Choose one of these providers:

**Option A: Anthropic Claude** (Recommended)
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```
Get key: https://console.anthropic.com/

**Option B: OpenAI GPT**
```bash
export OPENAI_API_KEY="sk-..."
```
Get key: https://platform.openai.com/

**Option C: OpenRouter** (Access to 100+ models - Recommended)
```bash
export OPENROUTER_API_KEY="sk-or-..."
export OPENROUTER_MODEL="anthropic/claude-sonnet-4.5"  # Optional (this is the default)
```
Get key: https://openrouter.ai/keys

Available OpenRouter models (2025):
- `anthropic/claude-sonnet-4.5` ⭐ (Default - Latest & Best)
- `anthropic/claude-haiku-4.5` (Fastest)
- `anthropic/claude-opus-4` (Smartest for complex tasks)
- `anthropic/claude-3.5-sonnet` (Previous generation)
- `openrouter/openai/gpt-4o`
- `openrouter/google/gemini-pro-1.5`
- See all 100+ models: https://openrouter.ai/models

### 3. Optional: Install Semantic Search (for better results)
```bash
pip install nexus-ai-fs[semantic-search-remote]
export OPENAI_API_KEY="sk-..."  # For embeddings
```

## Quick Examples

### CLI Usage

#### Basic Question Answering
```bash
# Ask a question about a document
nexus llm read /path/to/document.pdf "What are the key points?"

# Query multiple documents
nexus llm read "/docs/**/*.md" "How does authentication work?"
```

#### Different Models
```bash
# Use Claude
nexus llm read /doc.pdf "Summarize this" --model claude-sonnet-4

# Use GPT-4
nexus llm read /doc.pdf "Summarize this" --model gpt-4o

# Use OpenRouter
export OPENROUTER_API_KEY="sk-or-..."
nexus llm read /doc.pdf "Summarize this" --model openrouter/anthropic/claude-3.5-sonnet
```

#### Advanced Options
```bash
# Get detailed output with citations
nexus llm read /doc.pdf "Explain the API" --detailed

# Stream the response
nexus llm read /doc.pdf "Long analysis" --stream

# Use keyword search instead of semantic
nexus llm read /docs/*.md "API endpoints" --search-mode keyword

# Disable semantic search (read full document)
nexus llm read /doc.txt "Summarize" --no-search
```

#### Remote Server
```bash
# Set remote server URL
export NEXUS_URL="http://localhost:8080"
export NEXUS_API_KEY="your-api-key"

# Or use --remote-url flag
nexus llm read /doc.pdf "Question" --remote-url http://localhost:8080
```

### Python SDK Usage

#### Simple Usage
```python
from nexus import connect

async with connect() as nx:
    # Simple question - just get the answer
    answer = await nx.llm_read(
        path="/reports/q4.pdf",
        prompt="What were the top 3 challenges?",
        model="claude-sonnet-4"
    )
    print(answer)
```

#### Detailed Results
```python
# Get detailed result with citations and cost
result = await nx.llm_read_detailed(
    path="/docs/**/*.md",
    prompt="How does authentication work?",
    model="claude-sonnet-4"
)

print(result.answer)
print(f"\nSources: {len(result.citations)}")
for citation in result.citations:
    print(f"- {citation.path} (score: {citation.score:.2f})")
print(f"\nCost: ${result.cost:.4f}")
```

#### Streaming
```python
# Stream response for real-time output
async for chunk in nx.llm_read_stream(
    path="/report.pdf",
    prompt="Analyze the trends",
    model="claude-sonnet-4"
):
    print(chunk, end="", flush=True)
```

#### Custom System Prompt
```python
# Create custom reader with specific instructions
reader = nx.create_llm_reader(
    model="claude-sonnet-4",
    system_prompt="You are a technical documentation expert. Provide detailed, precise answers."
)

result = await reader.read(
    path="/docs/**/*.md",
    prompt="Explain the architecture"
)
print(result.answer)
```

#### OpenRouter Usage
```python
import os

# Set OpenRouter credentials
os.environ["OPENROUTER_API_KEY"] = "sk-or-..."

async with connect() as nx:
    answer = await nx.llm_read(
        path="/doc.pdf",
        prompt="Summarize this",
        model="openrouter/anthropic/claude-3.5-sonnet"
    )
    print(answer)
```

## Run Demo Scripts

### CLI Demo
```bash
# Set API key
export ANTHROPIC_API_KEY="sk-ant-..."
# OR
export OPENAI_API_KEY="sk-..."
# OR
export OPENROUTER_API_KEY="sk-or-..."
export OPENROUTER_MODEL="openrouter/anthropic/claude-3.5-sonnet"

# Run demo
./examples/cli/llm_document_reading_demo.sh

# Keep demo files for inspection
KEEP=1 ./examples/cli/llm_document_reading_demo.sh
```

### Python Demo
```bash
# Set API key
export ANTHROPIC_API_KEY="sk-ant-..."

# Run demo
python examples/py_demo/llm_document_reading_demo.py
```

### With Remote Server
```bash
# Start server
./scripts/init-nexus-with-auth.sh

# In another terminal, load credentials
source .nexus-admin-env

# Run demo
./examples/cli/llm_document_reading_demo.sh
```

## Common Use Cases

### 1. Document Q&A
```bash
nexus llm read /manual.pdf "How do I configure SSL certificates?"
```

### 2. Code Documentation
```bash
nexus llm read "/src/**/*.py" "What design patterns are used?"
```

### 3. Research Analysis
```bash
nexus llm read "/papers/**/*.pdf" "Compare the methodologies" --detailed
```

### 4. Report Summarization
```bash
nexus llm read "/reports/*.txt" "Summarize key metrics and trends" --stream
```

### 5. Multi-Language Support
```bash
nexus llm read /doc.pdf "请用中文总结这个文档" --model claude-sonnet-4
```

## Search Modes

### Semantic Search (default)
Uses embeddings for intelligent context retrieval
```bash
nexus llm read /docs/*.md "authentication" --search-mode semantic
```

### Keyword Search
Uses traditional text search (FTS)
```bash
nexus llm read /docs/*.md "JWT token" --search-mode keyword
```

### Hybrid Search
Combines both approaches
```bash
nexus llm read /docs/*.md "API security" --search-mode hybrid
```

### No Search
Reads entire document (for small files)
```bash
nexus llm read /config.yaml "Explain this" --no-search
```

## Cost Management

### Check Costs
```bash
# Use --detailed to see cost
nexus llm read /doc.pdf "Question" --detailed
```

### Output:
```
💰 Cost: $0.0045
🎯 Tokens: 1,234
```

### Reduce Costs
```bash
# Use smaller models
nexus llm read /doc.pdf "Question" --model claude-sonnet-4  # Cheaper

# Limit response length
nexus llm read /doc.pdf "Question" --max-tokens 200

# Use keyword search (no embeddings)
nexus llm read /doc.pdf "Question" --search-mode keyword
```

## Troubleshooting

### "No LLM API key found"
```bash
# Make sure you set one of these:
export ANTHROPIC_API_KEY="sk-ant-..."
export OPENAI_API_KEY="sk-..."
export OPENROUTER_API_KEY="sk-or-..."
```

### "Semantic search requires embedding provider"
```bash
# Install semantic search support
pip install nexus-ai-fs[semantic-search-remote]

# Or use keyword search
nexus llm read /doc.pdf "Question" --search-mode keyword

# Or disable search for small files
nexus llm read /doc.pdf "Question" --no-search
```

### "File not found"
```bash
# Use absolute paths or check current directory
nexus ls /workspace
nexus llm read /workspace/doc.pdf "Question"
```

## Next Steps

- **Index documents for semantic search:**
  ```bash
  nexus search index /workspace
  ```

- **Try different models:**
  - Claude: `claude-sonnet-4`, `claude-opus-4`
  - GPT: `gpt-4o`, `gpt-4o-mini`
  - Gemini: `gemini-2.0-flash-exp`
  - Via OpenRouter: 100+ models available

- **Build RAG applications:**
  See examples in `examples/py_demo/` for integration patterns

- **Explore advanced features:**
  - Custom chunking strategies
  - Multi-tenant document isolation
  - Permission-based access control
  - Response caching

## Support

- Documentation: https://docs.nexus.ai
- GitHub Issues: https://github.com/nexi-lab/nexus/issues
- Examples: `examples/` directory
