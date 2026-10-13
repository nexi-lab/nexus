import type { FetchClient } from "./fetch-client.js";
import type { RequestOptions } from "./types.js";
import { searchRequestOptions, type SearchConnection } from "./search-connection.js";
export type { SearchConnection } from "./search-connection.js";

/** Rust HTTP Search wire types. */
export interface SearchHit {
  readonly path: string;
  readonly zone_id: string;
  readonly chunk_index: number;
  readonly chunk_text: string;
  readonly score: number;
  readonly keyword_score?: number;
  readonly vector_score?: number;
  readonly title_score?: number;
  readonly expanded_context?: string;
  readonly mtime_ms?: number;
  readonly tier_boost?: number;
  readonly recency_boost?: number;
  readonly expansion_variant_index?: number;
}

export interface SearchResponse<T> {
  readonly results: readonly T[];
  readonly error?: string;
}

export interface SearchQuery {
  readonly q: string;
  readonly query_type?: "keyword" | "semantic" | "hybrid";
  readonly path_filter?: string;
  readonly limit?: number;
  readonly zone_id?: string;
}

export interface SearchBatchResponse<T> {
  readonly responses: readonly SearchResponse<T>[];
}

export interface DiscoveryStatus {
  readonly truncated: boolean;
  readonly error?: string;
}

export interface GlobResponse extends DiscoveryStatus {
  readonly paths: readonly string[];
}

export interface GrepResponse extends DiscoveryStatus {
  readonly matches: readonly GrepHit[];
}

export interface GrepHit {
  readonly path: string;
  readonly line_number: number;
  readonly line: string;
}

export interface SearchHealth {
  readonly status: string;
  readonly detail: string;
  readonly fts_writer_faults: number;
  readonly fts_writer_unavailable: number;
  readonly last_verified_commit_age_ms: number | null;
  readonly dispatch_panics: number;
}

export interface LocatedDocument {
  readonly indexed: boolean;
  readonly chunk_count: number;
  readonly mtime_ms: number | null;
  readonly zone_id: string;
}

export interface DiscoveryOptions {
  readonly maxResults?: number;
  /** Omission walks the subtree; [] searches nothing. */
  readonly files?: readonly string[];
}

/** Translates requests through the caller's existing authenticated transport. */
export class SearchClient {
  private readonly options: RequestOptions;

  constructor(
    private readonly transport: Pick<FetchClient, "get" | "post">,
    private readonly connection: SearchConnection = {},
  ) {
    this.options = searchRequestOptions(connection);
  }

  health(): Promise<SearchHealth> {
    return this.transport.get("/v2/search/health", this.options);
  }

  locate(path: string): Promise<LocatedDocument> {
    return this.transport.post("/v2/search/locate", {
      path, zone_id: this.connection.zoneId ?? "",
    }, this.options);
  }

  query(
    q: string,
    queryType: "keyword" | "semantic" | "hybrid" = "hybrid",
    pathFilter = "/",
    limit = 10,
  ): Promise<SearchResponse<SearchHit>> {
    return this.transport.post("/v2/search/query", {
      q, query_type: queryType, path_filter: pathFilter, limit,
      zone_id: this.connection.zoneId ?? "",
    }, this.options);
  }

  batchQuery(queries: readonly SearchQuery[]): Promise<SearchBatchResponse<SearchHit>> {
    return this.transport.post("/v2/search/query/batch", {
      queries: queries.map((query) => ({
        ...query, zone_id: query.zone_id ?? this.connection.zoneId ?? "",
      })),
    }, this.options);
  }

  private discoveryBody(pattern: string, rootPath: string, options: DiscoveryOptions) {
    return {
      pattern, root_path: rootPath, max_results: options.maxResults ?? 100,
      zone_id: this.connection.zoneId ?? "", files: options.files,
    };
  }

  glob(pattern: string, rootPath: string, options: DiscoveryOptions = {}): Promise<GlobResponse> {
    return this.transport.post("/v2/search/glob", this.discoveryBody(pattern, rootPath, options), this.options);
  }

  grep(pattern: string, rootPath: string, options: DiscoveryOptions = {}): Promise<GrepResponse> {
    return this.transport.post("/v2/search/grep", this.discoveryBody(pattern, rootPath, options), this.options);
  }
}
