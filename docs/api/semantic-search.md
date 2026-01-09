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
    adaptive_k: bool = False,
    context: OperationContext | EnhancedOperationContext | None = None
) -> list[dict[str, Any]]
```

**Parameters:**
- `query` (str): Natural language query (e.g., "How does authentication work?")
- `path` (str): Root path to search (default: "/")
- `limit` (int): Maximum number of results (default: 10). When `adaptive_k=True`, this is used as `k_base`.
- `filters` (dict, optional): Additional filters for search
- `search_mode` (str): Search mode - "keyword", "semantic", or "hybrid" (default: "semantic")
  - `"keyword"`: Fast keyword search using FTS (no embeddings needed)
  - `"semantic"`: Semantic search using vector embeddings
  - `"hybrid"`: Combines keyword + semantic for best results
- `adaptive_k` (bool): Enable adaptive retrieval depth based on query complexity (default: False). See [Adaptive Retrieval](#adaptive-retrieval-depth) below.
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

# Adaptive retrieval - automatically adjusts limit based on query complexity
results = await nx.semantic_search(
    "How does authentication compare to authorization?",
    limit=10,          # Used as k_base
    adaptive_k=True    # Complex query → limit increased to ~14
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

## Adaptive Retrieval Depth

Adaptive retrieval dynamically adjusts the number of results (`k`) based on query complexity, reducing token waste on simple queries while providing comprehensive context for complex ones.

This feature is inspired by [SimpleMem (arXiv:2601.02553)](https://arxiv.org/abs/2601.02553), which achieves 30-45% token savings on simple queries.

### Formula

```
k_dyn = ⌊k_base · (1 + δ · C_q)⌋
```

Where:
- `k_base`: Default retrieval count (the `limit` parameter)
- `δ` (delta): Scaling factor (default: 0.5)
- `C_q`: Query complexity score (0.0-1.0)

### Query Complexity Estimation

The complexity score is calculated based on multiple heuristics:

| Feature | Description | Score Impact |
|---------|-------------|--------------|
| **Word count** | Longer queries tend to be more complex | +0.0 to +0.25 |
| **Comparison keywords** | "vs", "compare", "differences" | +0.2 |
| **Temporal keywords** | "since", "before", "history" | +0.15 |
| **Aggregation keywords** | "all", "every", "summary" | +0.15 |
| **Multi-hop patterns** | "how does X affect Y" | +0.2 |
| **Complex questions** | "explain", "analyze" | +0.15 |
| **Simple questions** | "what is", "define" | -0.1 |

### Configuration

Configure adaptive retrieval using `AdaptiveRetrievalConfig`:

```python
from nexus.llm.context_builder import AdaptiveRetrievalConfig, ContextBuilder

config = AdaptiveRetrievalConfig(
    k_base=10,      # Default retrieval count
    k_min=3,        # Minimum results (never go below)
    k_max=20,       # Maximum results (never exceed)
    delta=0.5,      # Complexity scaling factor
    enabled=True    # Enable/disable adaptive retrieval
)

builder = ContextBuilder(adaptive_config=config)
```

### Examples

```python
# Enable adaptive k for search
results = await nx.semantic_search(
    "How does authentication compare to authorization in web security?",
    limit=10,         # Used as k_base
    adaptive_k=True   # Enable adaptive retrieval
)
# Complex query → limit automatically increased to ~14

# Simple query gets fewer results
results = await nx.semantic_search(
    "What is Python?",
    limit=10,
    adaptive_k=True
)
# Simple query → limit stays at ~10

# Calculate k manually
params = builder.get_retrieval_params("How does caching affect performance?")
print(f"Recommended k: {params['k']}")
print(f"Complexity score: {params['complexity_score']:.3f}")
```

### Benefits

- **30-45% token savings** on simple factual queries
- **Better context** for complex analytical queries
- **Automatic tuning** - no manual adjustment needed
- **Configurable bounds** - k_min and k_max ensure predictable behavior

---

## See Also

- [File Discovery](file-discovery.md) - Text-based search (grep, glob)
- [Memory Management](memory-management.md) - Memory storage
- [CLI Reference](cli-reference.md) - Search commands

## Next Steps

1. Initialize search with [configuration](configuration.md)
2. Index files with semantic_search_index()
3. Query with natural language
4. Enable adaptive_k for automatic result count optimization
