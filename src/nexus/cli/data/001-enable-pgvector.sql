-- Enable pgvector extension for vector similarity search (Issue #2663)
-- Required by txtai pgvector backend for hybrid BM25+dense search
CREATE EXTENSION IF NOT EXISTS vector;
