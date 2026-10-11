import { SearchClient, type FetchClient, type SearchConnection } from "@nexus-ai-fs/api-client";

export interface FileSearchHit {
  readonly path: string;
  readonly zoneId?: string;
  readonly line?: number;
  readonly content?: string;
}

export interface FileSearchResult {
  readonly hits: readonly FileSearchHit[];
  readonly truncated: boolean;
  readonly error?: string;
}

/** Search the current subtree; leave ranking and discovery to its owning host. */
export async function searchFiles(
  query: string,
  rootPath: string,
  transport: Pick<FetchClient, "get" | "post">,
  connection?: SearchConnection,
): Promise<FileSearchResult> {
  const search = new SearchClient(transport, connection);
  const mode = query.slice(0, 2);
  const text = (mode === "g:" || mode === "r:" ? query.slice(2) : query).trim();
  if (!text) return { hits: [], truncated: false };
  if (mode === "g:") {
    const response = await search.glob(text, rootPath);
    return { hits: response.paths.map(path => ({ path })), truncated: response.truncated, error: response.error };
  }
  if (mode === "r:") {
    const response = await search.grep(text, rootPath);
    return {
      hits: response.matches.map(hit => ({ path: hit.path, line: hit.line_number, content: hit.line })),
      truncated: response.truncated, error: response.error,
    };
  }
  const response = await search.query(text, "hybrid", rootPath, 100);
  return { hits: response.results.map(hit => ({ path: hit.path, zoneId: hit.zone_id })), truncated: false, error: response.error };
}
