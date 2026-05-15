-- Issue #3699: enable pgvector + pg_search.
-- pgvector → halfvec(1536) HNSW for dense semantic search.
-- pg_search → BM25 (@@@ / paradedb.score) for keyword search.
-- Both required by the post-txtai search stack (PgVectorBackend +
-- PgFtsBackend). Bundled by paradedb/paradedb image.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_search;
