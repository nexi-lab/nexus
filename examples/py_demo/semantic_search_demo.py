"""Semantic Search Demo - Keyword, Semantic, and Hybrid Search.

This demo shows the semantic search capabilities:
- Keyword-only search using FTS5/tsvector (no embeddings)
- Semantic search with OpenAI embeddings
- Hybrid search combining keyword + semantic
- Document indexing and chunking
- Search statistics
"""

import asyncio
import os
import tempfile
from pathlib import Path

import nexus


async def main() -> None:
    """Run the semantic search demo."""
    print("=" * 70)
    print("Nexus Semantic Search Demo - Keyword, Semantic, and Hybrid")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir) / "nexus-data"

        print(f"\n📁 Data directory: {data_dir}")

        # Initialize Nexus
        print("\n1. Connecting to Nexus...")
        nx = nexus.connect(config={"data_dir": str(data_dir)})
        print("   ✓ Connected")

        # ============================================================
        # Part 1: Create Sample Documents
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 1: Create Sample Documents")
        print("=" * 70)

        print("\n2. Creating sample documents...")

        # Create documents about different topics
        docs = {
            "/docs/python.md": """
# Python Programming

Python is a high-level, interpreted programming language known for its simplicity
and readability. It supports multiple programming paradigms including procedural,
object-oriented, and functional programming.

Python is widely used in web development, data science, machine learning, and
automation. Popular frameworks include Django, Flask, NumPy, and TensorFlow.
""",
            "/docs/javascript.md": """
# JavaScript Programming

JavaScript is a dynamic, interpreted programming language primarily used for
web development. It runs in browsers and enables interactive web pages.

JavaScript is essential for frontend development with frameworks like React,
Vue, and Angular. Node.js enables JavaScript on the server-side.
""",
            "/docs/databases.md": """
# Database Systems

Databases are organized collections of data. There are two main types:

1. SQL Databases (Relational): PostgreSQL, MySQL, SQLite
   - Use structured tables with relationships
   - ACID compliant
   - Use SQL query language

2. NoSQL Databases: MongoDB, Redis, Cassandra
   - Flexible schemas
   - Optimized for specific use cases
   - Horizontal scaling
""",
            "/docs/machine-learning.md": """
# Machine Learning

Machine learning is a subset of artificial intelligence that enables systems
to learn and improve from experience without explicit programming.

Key concepts include:
- Supervised Learning: Training with labeled data
- Unsupervised Learning: Finding patterns in unlabeled data
- Neural Networks: Deep learning architectures
- Natural Language Processing: Understanding text and language

Popular frameworks: TensorFlow, PyTorch, scikit-learn
""",
            "/docs/devops.md": """
# DevOps Practices

DevOps combines software development and IT operations to shorten the development
lifecycle and deliver high-quality software continuously.

Key practices:
- Continuous Integration/Continuous Deployment (CI/CD)
- Infrastructure as Code (IaC)
- Containerization with Docker and Kubernetes
- Monitoring and logging
- Automated testing
""",
        }

        for path, content in docs.items():
            nx.write(path, content.encode())
            print(f"   ✓ Created {path}")

        # ============================================================
        # Part 2: Keyword-Only Search (No Embeddings)
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 2: Keyword-Only Search (No Embeddings Required)")
        print("=" * 70)

        print("\n3. Initializing search engine (keyword-only mode)...")
        await nx.initialize_semantic_search()
        print("   ✓ Initialized with keyword search (FTS5/tsvector)")

        print("\n4. Indexing documents...")
        index_results = await nx.semantic_search_index(path="/docs", recursive=True)
        total_chunks = sum(index_results.values())
        print(f"   ✓ Indexed {len(index_results)} documents")
        print(f"   ✓ Created {total_chunks} chunks")

        print("\n5. Performing keyword search: 'database SQL'...")
        results = await nx.semantic_search(
            query="database SQL", path="/docs", limit=3, search_mode="keyword"
        )

        print(f"\n   Found {len(results)} results:")
        for i, result in enumerate(results, 1):
            print(f"\n   Result {i}:")
            print(f"   - File: {result['path']}")
            print(f"   - Score: {result['score']:.3f}")
            print(f"   - Preview: {result['chunk_text'][:100]}...")

        # ============================================================
        # Part 3: Semantic Search with OpenAI (Optional)
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 3: Semantic Search with OpenAI Embeddings (Optional)")
        print("=" * 70)

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            print("\n⚠️  OPENAI_API_KEY not set - skipping semantic search demo")
            print("   To enable semantic search:")
            print("   1. Install: pip install nexus-ai-fs[semantic-search-remote]")
            print("   2. Set: export OPENAI_API_KEY=sk-...")
            print("   3. Re-run this demo")
        else:
            print("\n6. Re-initializing with OpenAI embeddings...")
            await nx.initialize_semantic_search(embedding_provider="openai", api_key=api_key)
            print("   ✓ Initialized with OpenAI embeddings")

            print("\n7. Re-indexing documents with embeddings...")
            index_results = await nx.semantic_search_index(path="/docs", recursive=True)
            total_chunks = sum(index_results.values())
            print(f"   ✓ Indexed {len(index_results)} documents")
            print(f"   ✓ Created {total_chunks} chunks with embeddings")

            print("\n8. Semantic search: 'AI and neural networks'...")
            results = await nx.semantic_search(
                query="AI and neural networks", path="/docs", limit=3, search_mode="semantic"
            )

            print(f"\n   Found {len(results)} results:")
            for i, result in enumerate(results, 1):
                print(f"\n   Result {i}:")
                print(f"   - File: {result['path']}")
                print(f"   - Score: {result['score']:.3f}")
                print(f"   - Preview: {result['chunk_text'][:100]}...")

            # ============================================================
            # Part 4: Hybrid Search (Best Results)
            # ============================================================
            print("\n" + "=" * 70)
            print("PART 4: Hybrid Search (Keyword + Semantic)")
            print("=" * 70)

            print("\n9. Hybrid search: 'web development frameworks'...")
            results = await nx.semantic_search(
                query="web development frameworks", path="/docs", limit=3, search_mode="hybrid"
            )

            print(f"\n   Found {len(results)} results:")
            for i, result in enumerate(results, 1):
                print(f"\n   Result {i}:")
                print(f"   - File: {result['path']}")
                print(f"   - Combined Score: {result['score']:.3f}")
                if result.get("keyword_score"):
                    print(f"   - Keyword Score: {result['keyword_score']:.3f}")
                if result.get("vector_score"):
                    print(f"   - Semantic Score: {result['vector_score']:.3f}")
                print(f"   - Preview: {result['chunk_text'][:100]}...")

        # ============================================================
        # Part 5: Search Statistics
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 5: Search Statistics")
        print("=" * 70)

        print("\n10. Getting search statistics...")
        stats = await nx.semantic_search_stats()

        print("\n   Search Index Statistics:")
        print(f"   - Total Chunks: {stats['total_chunks']}")
        print(f"   - Indexed Files: {stats['indexed_files']}")
        print(f"   - Embedding Model: {stats['embedding_model'] or 'None (keyword-only)'}")
        print(f"   - Chunk Size: {stats['chunk_size']} tokens")
        print(f"   - Chunk Strategy: {stats['chunk_strategy']}")
        print(f"   - Database Type: {stats['database_type']}")
        print("\n   Search Capabilities:")
        print(f"   - Keyword Search: {stats['search_capabilities']['keyword']}")
        print(f"   - Semantic Search: {stats['search_capabilities']['semantic']}")
        print(f"   - Hybrid Search: {stats['search_capabilities']['hybrid']}")

        # Cleanup
        nx.close()

        print("\n" + "=" * 70)
        print("Demo Complete!")
        print("=" * 70)

        print("\n📝 Summary:")
        print("   ✓ Keyword-only search works out-of-the-box (no API keys)")
        print("   ✓ Semantic search requires OpenAI API key")
        print("   ✓ Hybrid search combines best of both approaches")
        print("   ✓ All search modes use existing database (SQLite/PostgreSQL)")
        print("\n💡 Next steps:")
        print("   - Try with your own documents")
        print("   - Experiment with different chunk sizes/strategies")
        print("   - Use hybrid search for best results")


if __name__ == "__main__":
    asyncio.run(main())
