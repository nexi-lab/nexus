/**
 * Column search results sub-view.
 * Displays datasets matching a column name query via the knowledge store.
 * Issue #2930.
 */

import React from "react";

interface ColumnResult {
  readonly entityUrn: string;
  readonly columnName: string;
  readonly columnType: string;
}

interface ColumnSearchProps {
  readonly results: readonly ColumnResult[];
  readonly loading: boolean;
}

export function ColumnSearch({
  results,
  loading,
}: ColumnSearchProps): React.ReactNode {
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
        {"  Column              Type         Entity URN"}
      </text>
      {results.slice(0, 30).map((r, i) => (
        <text key={i}>
          {`  ${r.columnName.padEnd(20)} ${r.columnType.padEnd(12)} ${r.entityUrn}`}
        </text>
      ))}
      {results.length > 30 && (
        <text>
          {`  ... and ${results.length - 30} more results`}
        </text>
      )}
    </box>
  );
}
