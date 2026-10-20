import type { FetchClient } from "./fetch-client.js";
import type { RequestOptions } from "./types.js";
import { createHttpError, NexusApiError } from "./errors.js";

export interface FileMetadata {
  readonly path: string;
  readonly size: number;
  readonly mime_type: string;
  readonly is_directory: boolean;
  readonly version: number;
  readonly gen: number;
  readonly zone_id: string;
  readonly created_at_ms: number | null;
  readonly modified_at_ms: number | null;
  readonly owner_id: string;
}
export interface WriteReceipt { readonly size: number; readonly gen: number; readonly content_id: string }

const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const unsigned = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const timestamp = (value: unknown) => value === null || (typeof value === "number" && Number.isSafeInteger(value));
const metadata = (value: unknown): value is FileMetadata => record(value)
  && ["path", "mime_type", "zone_id", "owner_id"].every((name) => typeof value[name] === "string")
  && typeof value["is_directory"] === "boolean"
  && [value["size"], value["version"], value["gen"]].every(unsigned)
  && timestamp(value["created_at_ms"]) && timestamp(value["modified_at_ms"]);

function checked<T>(value: unknown, valid: (value: unknown) => value is T): T {
  if (!valid(value)) throw new NexusApiError("Invalid native file response", 502, "invalid_response", value);
  return value;
}

export class VfsClient {
  constructor(private readonly client: Pick<FetchClient, "get" | "post" | "delete" | "rawRequest">) {}

  async list(path: string, options?: RequestOptions): Promise<readonly FileMetadata[]> {
    const result = await this.client.get<unknown>("/v2/directories", { ...options, query: { path }, transformKeys: false });
    return checked(result, (value): value is { entries: FileMetadata[] } => record(value)
      && Array.isArray(value["entries"]) && value["entries"].every(metadata)).entries;
  }

  async metadata(path: string, options?: RequestOptions): Promise<FileMetadata> {
    return checked(await this.client.get<unknown>("/v2/files/metadata", { ...options, query: { path }, transformKeys: false }), metadata);
  }

  async read(path: string, options?: RequestOptions): Promise<Uint8Array> {
    const response = await this.client.rawRequest("GET", "/v2/files", undefined, { ...options, query: { path } });
    await this.requireSuccess(response);
    return new Uint8Array(await response.arrayBuffer());
  }

  async write(path: string, content: string | Uint8Array, options?: RequestOptions): Promise<WriteReceipt> {
    const bytes = typeof content === "string" ? new TextEncoder().encode(content) : content;
    const response = await this.client.rawRequest("PUT", "/v2/files", bytes, {
      ...options, query: { path }, headers: { ...options?.headers, "Content-Type": "application/octet-stream" },
    });
    await this.requireSuccess(response);
    return checked(await response.json(), (value): value is WriteReceipt => record(value)
      && unsigned(value["size"]) && unsigned(value["gen"]) && typeof value["content_id"] === "string");
  }

  async delete(path: string, recursive = false, options?: RequestOptions): Promise<boolean> {
    const result = await this.client.delete<unknown>("/v2/files", { ...options, query: { path, recursive }, transformKeys: false });
    return checked(result, (value): value is { deleted: boolean } => record(value) && typeof value["deleted"] === "boolean").deleted;
  }

  async mkdir(path: string, options?: RequestOptions): Promise<void> {
    const result = await this.client.post<unknown>("/v2/directories", { path, parents: true, exist_ok: true }, { ...options, transformKeys: false });
    checked(result, (value): value is { hit: true } => record(value) && value["hit"] === true);
  }

  async rename(path: string, newPath: string, options?: RequestOptions): Promise<void> {
    const result = await this.client.post<unknown>("/v2/files/rename", { path, new_path: newPath }, { ...options, transformKeys: false });
    checked(result, (value): value is { success: true; is_directory: boolean } => record(value)
      && value["success"] === true && typeof value["is_directory"] === "boolean");
  }

  private async requireSuccess(response: Response): Promise<void> {
    if (response.ok) return;
    let body: unknown;
    try { body = await response.json(); } catch { body = null; }
    const message = record(body) && typeof body["error"] === "string" ? body["error"] : `HTTP ${response.status}`;
    throw createHttpError(response.status, message, body);
  }
}
