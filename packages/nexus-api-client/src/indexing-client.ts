import type { FetchClient } from "./fetch-client.js";
import { searchRequestOptions, type SearchConnection } from "./search-connection.js";
import type { RequestOptions } from "./types.js";

export interface IndexedDirectory {
  readonly path: string;
  readonly zone_id: string;
  readonly added_at_ms: number;
}

export interface IndexedDirectories {
  readonly directories: readonly IndexedDirectory[];
  readonly error?: string;
}

export type IndexingMode = "on" | "off" | "sandbox";

export interface IndexingModes {
  readonly modes: readonly { readonly zone_id: string; readonly mode: IndexingMode }[];
  readonly error?: string;
}

export interface IndexStatistics {
  readonly fts_doc_count: number;
  readonly fts_path_count: number;
  readonly ann_chunk_count: number;
  readonly parked_count: number;
  readonly backend: string;
  readonly embedding_model: string;
  readonly indexing_in_progress: number;
  readonly last_index_seq: number;
  readonly pending: number;
  readonly last_successful_index_at_ms: number;
  readonly error?: string;
}

/** Index configuration and diagnostics through the caller's authenticated host transport. */
export class IndexingClient {
  private readonly options: RequestOptions;

  constructor(
    private readonly transport: Pick<FetchClient, "get" | "post" | "put" | "delete">,
    private readonly connection: SearchConnection = {},
  ) {
    this.options = searchRequestOptions(connection);
  }

  stats(): Promise<IndexStatistics> {
    return this.transport.get("/v2/documents/stats", {
      ...this.options, query: { zone_id: this.connection.zoneId ?? "" },
    });
  }

  listDirectories(): Promise<IndexedDirectories> {
    return this.transport.get("/v2/documents/directories", {
      ...this.options, query: { zone_id: this.connection.zoneId ?? "" },
    });
  }

  addDirectory(path: string): Promise<{ readonly added: boolean; readonly error?: string }> {
    return this.transport.post("/v2/documents/directories", {
      path, zone_id: this.connection.zoneId ?? "",
    }, this.options);
  }

  removeDirectory(path: string): Promise<{ readonly removed: boolean; readonly error?: string }> {
    return this.transport.delete("/v2/documents/directories", {
      ...this.options, query: { path, zone_id: this.connection.zoneId ?? "" },
    });
  }

  /** Empty mode resets the host default. */
  setIndexingMode(mode: IndexingMode | ""): Promise<{ readonly error?: string }> {
    return this.transport.put("/v2/documents/indexing-modes", {
      mode, zone_id: this.connection.zoneId ?? "",
    }, this.options);
  }

  listIndexingModes(): Promise<IndexingModes> {
    return this.transport.get("/v2/documents/indexing-modes", this.options);
  }
}
