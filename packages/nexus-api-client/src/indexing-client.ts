import type { FetchClient } from "./fetch-client.js";
import { searchRequestOptions, type SearchConnection } from "./search-connection.js";
import type { RequestOptions } from "./types.js";
import { NexusApiError } from "./errors.js";

export interface IndexDocument {
  readonly path: string;
  readonly text: string;
  readonly mtime_ms?: number;
  readonly zone_id?: string;
}

export interface DocumentBatchResult {
  readonly indexed_count: number;
  readonly skipped_count: number;
  readonly parked_paths: readonly string[];
  readonly skipped_paths: readonly string[];
  readonly index_seq: number;
}

export interface FileIndexResult {
  readonly status: "indexed" | "skipped";
  readonly reason?: string;
  readonly index_seq: number;
  readonly zone_id: string;
}

export interface FileDeleteResult {
  readonly status: "deleted";
  readonly index_seq: number;
  readonly zone_id: string;
}

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

/** Index mutations, configuration and diagnostics through the caller's authenticated host. */
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

  indexDocuments(documents: readonly IndexDocument[]): Promise<DocumentBatchResult> {
    return this.transport.post("/v2/documents/batch", {
      documents, zone_id: this.connection.zoneId ?? "",
    }, this.options);
  }

  async indexFile(path: string): Promise<FileIndexResult> {
    try {
      return await this.transport.post("/v2/documents/file", {
        path, zone_id: this.connection.zoneId ?? "",
      }, this.options);
    } catch (error) {
      if (error instanceof NexusApiError && error.status === 409
          && error.body && typeof error.body === "object") {
        const result = error.body as Partial<FileIndexResult>;
        if (result.status === "skipped" && typeof result.reason === "string"
            && typeof result.index_seq === "number" && typeof result.zone_id === "string") {
          return result as FileIndexResult;
        }
      }
      throw error;
    }
  }

  deleteFile(path: string): Promise<FileDeleteResult> {
    return this.transport.delete("/v2/documents/file", {
      ...this.options, query: { path, zone_id: this.connection.zoneId ?? "" },
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
