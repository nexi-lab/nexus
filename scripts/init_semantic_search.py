#!/usr/bin/env python3
"""
Initialize semantic search for Nexus.

This script initializes semantic search with either:
- Keyword-only mode (default, uses PostgreSQL FTS)
- Semantic mode with OpenAI embeddings (if OPENAI_API_KEY is set)

Usage:
    python scripts/init_semantic_search.py [semantic_mode]

Environment variables:
    NEXUS_DATA_DIR: Data directory (default: /app/data)
    NEXUS_DATABASE_URL: Database connection URL
    NEXUS_SEMANTIC_MODE: 'keyword' or 'semantic' (default: 'keyword')
    OPENAI_API_KEY: OpenAI API key (required for semantic mode)
"""

import asyncio
import os
import sys
from pathlib import Path

# Add src to path for imports
script_dir = Path(__file__).parent
src_dir = script_dir.parent / "src"
sys.path.insert(0, str(src_dir))

from nexus.backends.local import LocalBackend  # noqa: E402
from nexus.core.nexus_fs import NexusFS  # noqa: E402
from nexus.storage.raft_metadata_store import RaftMetadataStore  # noqa: E402


async def init_semantic_search() -> bool:
    """
    Initialize semantic search (defaults to keyword-only for safety).

    Returns:
        True if successful, False otherwise
    """
    try:
        data_dir = os.getenv("NEXUS_DATA_DIR", "/app/data")
        database_url = os.getenv("NEXUS_DATABASE_URL")

        if not database_url:
            print("ERROR: NEXUS_DATABASE_URL is required", file=sys.stderr)
            return False

        backend = LocalBackend(data_dir)
        metadata_store = RaftMetadataStore.local(str(database_url).replace(".db", ""))
        nx = NexusFS(backend, metadata_store=metadata_store)

        # Check if explicitly requested to use vector embeddings
        # Default: keyword-only mode (safer, more stable)
        semantic_mode = os.getenv("NEXUS_SEMANTIC_MODE", "keyword")

        if semantic_mode == "semantic":
            # Only use embeddings if explicitly requested
            openai_api_key = os.getenv("OPENAI_API_KEY")
            if openai_api_key and openai_api_key != "your-openai-api-key":
                await nx.initialize_semantic_search(
                    embedding_provider="openai",
                    api_key=openai_api_key,
                    chunk_size=512,
                    chunk_strategy="semantic",
                )
                print(
                    "✓ Semantic search initialized (OpenAI embeddings - experimental)",
                    file=sys.stderr,
                )
            else:
                print(
                    "WARNING: NEXUS_SEMANTIC_MODE=semantic but no valid OPENAI_API_KEY found",
                    file=sys.stderr,
                )
                print("Falling back to keyword-only mode", file=sys.stderr)
                await nx.initialize_semantic_search(
                    embedding_provider=None,
                    chunk_size=512,
                    chunk_strategy="semantic",
                )
                print("✓ Semantic search initialized (keyword-only mode)", file=sys.stderr)
        else:
            # Default: keyword-only mode (PostgreSQL FTS)
            await nx.initialize_semantic_search(
                embedding_provider=None,
                chunk_size=512,
                chunk_strategy="semantic",
            )
            print("✓ Semantic search initialized (keyword-only mode)", file=sys.stderr)

        nx.close()
        return True

    except Exception as e:
        print(f"ERROR: Failed to initialize semantic search: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return False


def main() -> None:
    """Main entry point."""
    success = asyncio.run(init_semantic_search())
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
