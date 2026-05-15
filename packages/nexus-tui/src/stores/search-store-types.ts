/**
 * Type definitions for the Search & Knowledge panel store.
 *
 * Extracted from search-store.ts to keep the store file focused on
 * state management and actions.
 */

// =============================================================================
// Public types (snake_case matching API wire format)
// =============================================================================

export interface SearchResult {
  readonly path: string;
  readonly chunk_text: string;
  readonly score: number;
  readonly chunk_index: number;
  readonly line_start: number | null;
  readonly line_end: number | null;
  readonly keyword_score: number | null;
  readonly vector_score: number | null;
  readonly splade_score?: number | null;
  readonly reranker_score?: number | null;
}

export interface KnowledgeEntity {
  readonly [key: string]: unknown;
}

export interface NeighborEntry {
  readonly entity: KnowledgeEntity;
  readonly depth: number;
  readonly path: readonly string[];
}

export interface Memory {
  readonly [key: string]: unknown;
}

/** Matches backend PlaybookResponse from playbook.py. */
export interface PlaybookRecord {
  readonly playbook_id: string;
  readonly name: string;
  readonly description: string | null;
  readonly version: number;
  readonly scope: string;
  readonly visibility: string;
  readonly usage_count: number;
  readonly success_rate: number | null;
  readonly strategies: readonly unknown[] | null;
  readonly created_at: string | null;
  readonly updated_at: string | null;
}

export interface MemoryVersion {
  readonly version: number;
  readonly created_at: string;
  readonly status: string;
}

export interface MemoryHistory {
  readonly memory_id: string;
  readonly current_version: number;
  readonly versions: readonly MemoryVersion[];
}

export interface MemoryDiff {
  readonly diff: string;
  readonly mode: string;
  readonly v1: number;
  readonly v2: number;
}

/** A single RLM inference iteration (rlm.iteration SSE event). */
export interface RlmStep {
  readonly step: number;
  readonly code_executed: string;
  readonly output_summary: string;
  readonly tokens_used: number;
  readonly duration_seconds: number;
}

/** Progressive state of an RLM streaming inference. */
export interface RlmAnswer {
  readonly status: "streaming" | "completed" | "budget_exceeded" | "error";
  readonly answer: string | null;
  readonly total_tokens: number;
  readonly total_duration_seconds: number;
  readonly iterations: number;
  readonly error_message: string | null;
  readonly steps: readonly RlmStep[];
  readonly model: string | null;
}

export type SearchTab = "search" | "knowledge" | "memories" | "playbooks" | "ask" | "columns";
export type SearchMode = "keyword" | "semantic" | "hybrid";

// =============================================================================
// Internal response types (used only by the store implementation)
// =============================================================================

export interface SearchQueryResponse {
  readonly query: string;
  readonly search_type: string;
  readonly graph_mode: string;
  readonly results: readonly SearchResult[];
  readonly total: number;
  readonly latency_ms: number;
}

export interface EntityResponse {
  readonly entity: KnowledgeEntity | null;
}

export interface NeighborsResponse {
  readonly neighbors: readonly NeighborEntry[];
}

export interface KnowledgeSearchResponse {
  readonly entity: KnowledgeEntity | null;
}

export interface MemorySearchResponse {
  readonly memories: readonly Memory[];
}

export interface MemoryDetailResponse {
  readonly memory: Memory;
}

export interface PlaybooksListResponse {
  readonly playbooks: readonly PlaybookRecord[];
  readonly total: number;
}

export interface MemoryHistoryResponse {
  readonly memory_id: string;
  readonly current_version: number;
  readonly versions: readonly MemoryVersion[];
}

export interface MemoryDiffResponse {
  readonly diff: string;
  readonly mode: string;
  readonly v1: number;
  readonly v2: number;
}

/** Matches backend rollback response from memories.py:480. */
export interface MemoryRollbackResponse {
  readonly rolled_back: boolean;
  readonly memory_id: string;
  readonly rolled_back_to_version: number;
  readonly current_version: number | null;
  readonly content: unknown;
}
