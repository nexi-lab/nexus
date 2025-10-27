# CLI: Semantic Search

← [CLI Reference](index.md) | [API Documentation](../README.md)

This document describes CLI commands for semantic search operations and their Python API equivalents.

Semantic search enables finding documents by meaning rather than exact keyword matches, using AI embeddings.

## search init - Initialize semantic search

Initialize the semantic search system with an embedding provider.

**CLI:**
```bash
# Initialize with OpenAI
nexus search init --provider openai --model text-embedding-3-small

# Initialize with mock provider (testing)
nexus search init --provider mock
```

**Python API:**
```python
# Initialize with OpenAI
nx.init_semantic_search(provider="openai", model="text-embedding-3-small")

# Initialize with mock provider
nx.init_semantic_search(provider="mock")

# With custom API key
nx.init_semantic_search(
    provider="openai",
    model="text-embedding-3-small",
    api_key="your-api-key"
)
```

**Options:**
- `--provider TEXT`: Embedding provider (openai, mock)
- `--model TEXT`: Model name (e.g., text-embedding-3-small)
- `--api-key TEXT`: API key for the provider

**See Also:**
- [Python API: init_semantic_search()](../semantic-search.md#init_semantic_search)

---

## search index - Index documents

Index documents for semantic search.

**CLI:**
```bash
# Index all documents
nexus search index

# Index specific path
nexus search index /docs

# Index single file
nexus search index /docs/README.md
```

**Python API:**
```python
# Index all documents
await nx.index_documents()

# Index specific path
await nx.index_documents(path="/docs")

# Index single file
await nx.index_documents(path="/docs/README.md")

# Index with progress tracking
async def index_with_progress():
    result = await nx.index_documents(path="/docs")
    print(f"Indexed {result['indexed_count']} documents")
    print(f"Failed: {result['failed_count']}")
```

**Options:**
- `path`: Path to index (default: all documents)

**See Also:**
- [Python API: index_documents()](../semantic-search.md#index_documents)

---

## search query - Search documents

Search indexed documents semantically.

**CLI:**
```bash
# Basic search
nexus search query "How does authentication work?"

# Limit results
nexus search query "database migration" --limit 5

# Search in specific path
nexus search query "API endpoints" --path /docs
```

**Python API:**
```python
# Basic search (async)
import asyncio

async def search():
    results = await nx.semantic_search("How does authentication work?")
    for result in results:
        print(f"{result['path']}: {result['score']}")
        print(f"  {result['snippet']}")

asyncio.run(search())

# Limit results
async def search_top():
    results = await nx.semantic_search("database migration", limit=5)
    return results

# Search in specific path
async def search_docs():
    results = await nx.semantic_search("API endpoints", path="/docs")
    for result in results:
        print(f"{result['path']}: {result['content'][:100]}...")
```

**Options:**
- `--limit NUM`: Maximum number of results (default: 10)
- `--path TEXT`: Search within specific path

**See Also:**
- [Python API: semantic_search()](../semantic-search.md#semantic_search)

---

## search stats - Show statistics

Display semantic search indexing statistics.

**CLI:**
```bash
# Show indexing stats
nexus search stats
```

**Python API:**
```python
# Get search statistics
stats = nx.get_search_stats()
print(f"Indexed documents: {stats['document_count']}")
print(f"Total embeddings: {stats['embedding_count']}")
print(f"Last indexed: {stats['last_indexed_at']}")
```

**See Also:**
- [Python API: get_search_stats()](../semantic-search.md#get_search_stats)

---

## Common Workflows

### Set up semantic search
```bash
# Initialize search
nexus search init --provider openai --model text-embedding-3-small

# Index all documents
nexus search index /

# Check stats
nexus search stats

# Search
nexus search query "authentication best practices"
```

### Python equivalent
```python
import nexus
import asyncio

# Initialize
nx = nexus.Nexus(data_dir="./nexus-data")

# Set up semantic search
nx.init_semantic_search(provider="openai", model="text-embedding-3-small")

# Index documents
async def setup_search():
    # Index all documents
    result = await nx.index_documents(path="/")
    print(f"Indexed {result['indexed_count']} documents")

    # Check stats
    stats = nx.get_search_stats()
    print(f"Total documents: {stats['document_count']}")

    # Search
    results = await nx.semantic_search("authentication best practices")
    for result in results:
        print(f"\n{result['path']} (score: {result['score']:.3f})")
        print(result['snippet'])

asyncio.run(setup_search())
```

### Incremental indexing
```bash
# Index new documentation
nexus search index /docs/new

# Update existing index
nexus search index /docs/updated-file.md

# Search updated content
nexus search query "new feature documentation"
```

### Python equivalent
```python
async def incremental_indexing():
    # Index new documentation
    await nx.index_documents(path="/docs/new")

    # Update existing index
    await nx.index_documents(path="/docs/updated-file.md")

    # Search updated content
    results = await nx.semantic_search("new feature documentation")
    for result in results:
        print(f"{result['path']}: {result['score']:.3f}")

asyncio.run(incremental_indexing())
```

---

## See Also

- [CLI Reference Overview](index.md)
- [Python API: Semantic Search](../semantic-search.md)
- [Memory Management](memory.md)
- [Search Operations](search.md)
