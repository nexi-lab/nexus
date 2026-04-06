import type { JSX } from "solid-js";
/**
 * Column search results sub-view.
 * Displays datasets matching a column name query via the knowledge store.
 * Issue #2930.
 */

interface ColumnResult {
  readonly entityUrn: string;
  readonly columnName: string;
  readonly columnType: string;
  readonly path?: string | null;
  readonly schema?: {
    readonly columns?: readonly { name: string; type: string }[];
    readonly format?: string;
    readonly row_count?: number;
  };
}

interface ColumnSearchProps {
  readonly results: readonly ColumnResult[];
  readonly loading: boolean;
}

function formatUrn(urn: string): string {
  // urn:nexus:file:zone:hash → show shortened hash
  const parts = urn.split(":");
  const hash = parts[parts.length - 1] ?? urn;
  const zone = parts.length >= 4 ? parts[3] : "";
  return zone ? `${zone}:${hash.slice(0, 8)}…` : hash.slice(0, 12) + "…";
}

export function ColumnSearch({
  results,
  loading,
}: ColumnSearchProps): JSX.Element {
  if (loading) {
    return <text>Searching columns...</text>;
  }

  if (results.length === 0) {
    return (
      <text>
        No column matches. Press / and type a column name to search.
      </text>
    );
  }

  return (
    <box flexDirection="column" height="100%" width="100%">
      <text>
        {"  COLUMN              TYPE         FILE                                    FORMAT  ROWS  OTHER COLUMNS"}
      </text>
      <text>
        {"  ──────────────────  ───────────  ──────────────────────────────────────  ──────  ────  ─────────────────────────"}
      </text>
      {results.slice(0, 30).map((r, i) => {
        const format = r.schema?.format ?? "?";
        const rows = r.schema?.row_count != null ? String(r.schema.row_count) : "?";
        const filePath = r.path ?? "(unknown)";
        const otherCols = (r.schema?.columns ?? [])
          .filter((c) => c.name !== r.columnName)
          .map((c) => c.name)
          .join(", ");
        return (
          <text>
            {`  ${r.columnName.padEnd(20)} ${r.columnType.padEnd(12)} ${filePath.padEnd(40)} ${format.padEnd(7)} ${rows.padEnd(5)} ${otherCols}`}
          </text>
        );
      })}
      {results.length > 30 && (
        <text>
          {`  ... and ${results.length - 30} more results`}
        </text>
      )}
    </box>
  );
}
