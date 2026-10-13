import type { RequestOptions } from "./types.js";

export interface SearchConnection {
  /** Owning host's HTTP URL; omitted when it shares the transport's origin. */
  readonly url?: string;
  /** Explicit zone. Query omission selects readable zones; index configuration defaults to root. */
  readonly zoneId?: string;
}

export function searchRequestOptions(connection: SearchConnection): RequestOptions {
  return { transformKeys: false, ...(connection.url ? { baseUrl: connection.url } : {}) };
}
