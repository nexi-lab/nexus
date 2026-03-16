/**
 * Schema sub-view for the Files panel.
 * Shows extracted column schema for CSV/JSON/Parquet files.
 * Issue #2930.
 */

import React, { useEffect } from "react";
import type { FileItem } from "../../stores/files-store.js";
import { useKnowledgeStore } from "../../stores/knowledge-store.js";
import { useApi } from "../../shared/hooks/use-api.js";

interface FileSchemaProps {
  readonly item: FileItem | null;
}

const DATA_EXTENSIONS = new Set([
  "csv",
  "tsv",
  "json",
  "jsonl",
  "ndjson",
  "parquet",
  "pq",
]);

function isDataFile(item: FileItem): boolean {
  if (item.isDirectory) return false;
  const ext = item.name.split(".").pop()?.toLowerCase() ?? "";
  return DATA_EXTENSIONS.has(ext);
}

export function FileSchema({ item }: FileSchemaProps): React.ReactNode {
  const client = useApi();
  const schemaCache = useKnowledgeStore((s) => s.schemaCache);
  const loading = useKnowledgeStore((s) => s.schemaLoading);
  const fetchSchema = useKnowledgeStore((s) => s.fetchSchema);

  useEffect(() => {
    if (client && item && isDataFile(item)) {
      fetchSchema(item.path, client);
    }
  }, [client, item, fetchSchema]);

  if (!item) {
    return <text>No file selected</text>;
  }

  if (!isDataFile(item)) {
    return (
      <box flexDirection="column" height="100%" width="100%">
        <text>{"─── Schema ───"}</text>
        <text>{"Not a data file (CSV/JSON/Parquet)"}</text>
      </box>
    );
  }

  if (loading) {
    return <text>Extracting schema...</text>;
  }

  const schema = schemaCache.get(item.path);

  if (schema === undefined) {
    return <text>{"Schema not loaded yet"}</text>;
  }

  if (schema === null) {
    return (
      <box flexDirection="column" height="100%" width="100%">
        <text>{"─── Schema ───"}</text>
        <text>{"No schema available"}</text>
      </box>
    );
  }

  return (
    <box flexDirection="column" height="100%" width="100%">
      <text>{`─── Schema (${schema.format}) ───`}</text>
      <text>{`  Rows: ${schema.rowCount ?? "n/a"}  Confidence: ${(schema.confidence * 100).toFixed(0)}%`}</text>
      <text> </text>
      <text>{"  Column                Type         Nullable"}</text>
      {schema.columns.map((col) => (
        <text key={col.name}>
          {`  ${col.name.padEnd(20)} ${col.type.padEnd(12)} ${col.nullable}`}
        </text>
      ))}
    </box>
  );
}
