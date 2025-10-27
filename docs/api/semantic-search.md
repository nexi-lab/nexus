# Semantic Search

← [API Documentation](README.md)

This document describes semantic search capabilities using vector embeddings.

Nexus provides semantic search capabilities using vector embeddings for natural language queries.

### initialize_semantic_search()

Initialize semantic search with an embedding provider.

```python
async def initialize_semantic_search(
    embedding_provider: str = "openai",
    embedding_model: str = "text-embedding-3-small",
    chunk_size: int = 512,
    chunk_overlap: int = 50,
    **provider_kwargs
) -> None
```

**Parameters:**
- `embedding_provider` (str): Provider name - "openai", "sentence-transformers", or "mock"
- `embedding_model` (str): Model name for embeddings
- `chunk_size` (int): Size of text chunks for indexing (default: 512 tokens)
- `chunk_overlap` (int): Overlap between chunks (default: 50 tokens)
- `**provider_kwargs`: Provider-specific configuration (e.g., api_key, base_url)

**Examples:**

```python
# Initialize with OpenAI
await nx.initialize_semantic_search(
    embedding_provider="openai",
    embedding_model="text-embedding-3-small"
)

# Initialize with custom configuration
await nx.initialize_semantic_search(
    embedding_provider="openai",
    chunk_size=1024,
    chunk_overlap=100
)
```

---

### semantic_search()

Search documents using natural language queries with semantic understanding.

```python
async def semantic_search(
    query: str,
    path: str = "/",
    limit: int = 10,
    filters: dict[str, Any] | None = None,
    search_mode: str = "semantic",
    context: OperationContext | EnhancedOperationContext | None = None
) -> list[dict[str, Any]]
```

**Parameters:**
- `query` (str): Natural language query (e.g., "How does authentication work?")
- `path` (str): Root path to search (default: "/")
- `limit` (int): Maximum number of results (default: 10)
- `filters` (dict, optional): Additional filters for search
- `search_mode` (str): Search mode - "keyword", "semantic", or "hybrid" (default: "semantic")
  - `"keyword"`: Fast keyword search using FTS (no embeddings needed)
  - `"semantic"`: Semantic search using vector embeddings
  - `"hybrid"`: Combines keyword + semantic for best results
- `context` (OperationContext | EnhancedOperationContext, optional): Operation context for permission filtering (uses default if None)

**Returns:**
- `list[dict]`: Search results (filtered by READ permission) with keys:
  - `path`: File path
  - `chunk_index`: Index of the chunk in the document
  - `chunk_text`: Text content of the chunk
  - `score`: Relevance score (0.0 to 1.0)
  - `start_offset`: Start offset in document (optional)
  - `end_offset`: End offset in document (optional)

**Raises:**
- `ValueError`: If semantic search is not initialized

**Examples:**

```python
# Search for information about authentication
results = await nx.semantic_search("How does authentication work?")
for r in results:
    print(f"{r['path']}: {r['score']:.2f}")
    print(f"  {r['chunk_text'][:100]}...")

# Search only in documentation directory
results = await nx.semantic_search(
    "database migration",
    path="/docs",
    limit=5
)

# Search with permission filtering
from nexus.core.permissions import OperationContext
ctx = OperationContext(user="alice", groups=["engineering"])
results = await nx.semantic_search(
    "authentication",
    context=ctx  # Only returns files alice can read
)

# Hybrid search (keyword + semantic)
results = await nx.semantic_search(
    "error handling",
    search_mode="hybrid"
)
```

---

### semantic_search_index()

Index documents for semantic search by chunking and generating embeddings.

```python
async def semantic_search_index(
    path: str = "/",
    recursive: bool = True,
    context: OperationContext | EnhancedOperationContext | None = None
) -> dict[str, int]
```

**Parameters:**
- `path` (str): Path to index - can be a file or directory (default: "/")
- `recursive` (bool): If True, index directory recursively (default: True)
- `context` (OperationContext | EnhancedOperationContext, optional): Operation context for permission checks (uses default if None)

**Returns:**
- `dict[str, int]`: Mapping of file paths to number of chunks indexed

**Raises:**
- `ValueError`: If semantic search is not initialized
- `PermissionError`: If user doesn't have READ permission on files

**Examples:**

```python
# Index all documents
result = await nx.semantic_search_index()
print(f"Indexed {len(result)} files")

# Index specific directory
result = await nx.semantic_search_index("/docs", recursive=True)

# Index single file
result = await nx.semantic_search_index("/docs/README.md")
print(f"Created {result['/docs/README.md']} chunks")

# Index with specific context
from nexus.core.permissions import OperationContext
ctx = OperationContext(user="alice", groups=["engineering"])
result = await nx.semantic_search_index("/workspace", context=ctx)
```

---

### semantic_search_stats()

Get semantic search indexing statistics.

```python
async def semantic_search_stats(
    context: OperationContext | EnhancedOperationContext | None = None
) -> dict[str, Any]
```

**Parameters:**
- `context` (OperationContext | EnhancedOperationContext, optional): Operation context (uses default if None)

**Returns:**
- `dict`: Statistics with keys:
  - `total_chunks`: Total number of indexed chunks
  - `indexed_files`: Number of indexed files
  - `collection_name`: Name of the vector collection
  - `embedding_model`: Name of the embedding model
  - `chunk_size`: Chunk size in tokens
  - `chunk_strategy`: Chunking strategy

**Examples:**

```python
stats = await nx.semantic_search_stats()
print(f"Indexed {stats['indexed_files']} files")
print(f"Total chunks: {stats['total_chunks']}")
print(f"Model: {stats['embedding_model']}")
```

---

## See Also

- [File Discovery](file-discovery.md) - Text-based search (grep, glob)
- [Memory Management](memory-management.md) - Memory storage
- [CLI Reference](cli-reference.md) - Search commands

## Next Steps

1. Initialize search with [configuration](configuration.md)
2. Index files with semantic_search_index()
3. Query with natural language
