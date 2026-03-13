/**
 * Reindex status sub-view for the Zones panel.
 * Trigger and monitor index rebuild operations.
 * Issue #2930.
 */

import React, { useState } from "react";
import { useApi } from "../../shared/hooks/use-api.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";

interface ReindexResult {
  readonly target: string;
  readonly total: number;
  readonly processed: number;
  readonly errors: number;
  readonly lastSequence: number;
  readonly dryRun: boolean;
}

export function ReindexStatus(): React.ReactNode {
  const client = useApi();
  const [result, setResult] = useState<ReindexResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useKeyboard({
    d: () => {
      void triggerReindex("all", true);
    },
    r: () => {
      void triggerReindex("all", false);
    },
  });

  const triggerReindex = async (target: string, dryRun: boolean): Promise<void> => {
    if (!client) return;
    setLoading(true);
    setError(null);
    try {
      const res = await client.post<ReindexResult>("/api/v2/admin/reindex", {
        target,
        dryRun,
      });
      setResult(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Reindex failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <box flexDirection="column" height="100%" width="100%">
      <text>{"─── Reindex Operations ───"}</text>
      <text> </text>
      <text>{"  Targets: search | versions | semantic | all"}</text>
      <text>{"  Use CLI: nexus reindex --target search [--dry-run]"}</text>
      <text>{"  Press 'd' for dry-run, 'r' to reindex all"}</text>
      <text> </text>

      {loading && <text>{"  Reindex in progress..."}</text>}

      {error && <text>{`  Error: ${error}`}</text>}

      {result && (
        <box flexDirection="column">
          <text>{`  Last Reindex ${result.dryRun ? "(dry run)" : ""}`}</text>
          <text>{`  Target:     ${result.target}`}</text>
          <text>{`  Total:      ${result.total} MCL records`}</text>
          <text>{`  Processed:  ${result.processed}`}</text>
          <text>{`  Errors:     ${result.errors}`}</text>
          <text>{`  Last seq:   ${result.lastSequence}`}</text>
        </box>
      )}

      {!result && !loading && !error && (
        <text>{"  No reindex operations performed in this session."}</text>
      )}
    </box>
  );
}
