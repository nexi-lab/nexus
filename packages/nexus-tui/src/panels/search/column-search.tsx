import type { JSX } from "solid-js";
/**
 * Column search results sub-view.
 * Displays datasets matching a column name query via the knowledge store.
 * Issue #2930.
 */

import { useKnowledgeStore } from "../../stores/knowledge-store.js";

export function ColumnSearch(): JSX.Element {
  // Read directly from store for reactive updates (not props)
  const results = () => useKnowledgeStore((s) => s.columnSearchResults) as readonly {
    entityUrn: string; columnName: string; columnType: string;
    path?: string | null;
    schema?: { columns?: readonly { name: string; type: string }[]; format?: string; row_count?: number };
  }[];
  const loading = () => useKnowledgeStore((s) => s.columnSearchLoading);

  return (
    <box flexDirection="column" height="100%" width="100%">
      <text>{loading() ? "Searching columns..." : results().length === 0 ? "No column matches. Press / and type a column name to search." : `${results().length} column matches:`}</text>
      <text>{"  COLUMN              TYPE         FILE                                    FORMAT  ROWS  OTHER COLUMNS"}</text>
      <text>{"  ──────────────────  ───────────  ──────────────────────────────────────  ──────  ────  ─────────────────────────"}</text>
      {results().slice(0, 30).map((r) => {
        const format = r.schema?.format ?? "?";
        const rows = r.schema?.row_count != null ? String(r.schema.row_count) : "?";
        const filePath = r.path ?? "(unknown)";
        const otherCols = (r.schema?.columns ?? [])
          .filter((c) => c.name !== r.columnName)
          .map((c) => c.name)
          .join(", ");
        return (
          <text>{`  ${r.columnName.padEnd(20)} ${r.columnType.padEnd(12)} ${filePath.padEnd(40)} ${format.padEnd(7)} ${rows.padEnd(5)} ${otherCols}`}</text>
        );
      })}
    </box>
  );
}
