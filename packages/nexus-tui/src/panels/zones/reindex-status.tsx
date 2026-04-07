/**
 * Reindex status sub-view for the Zones panel.
 * Trigger and monitor index rebuild operations.
 * Issue #2930.
 */

import { createSignal } from "solid-js";
import type { JSX } from "solid-js";
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

export function ReindexStatus(): JSX.Element {
  const client = useApi();
  const [result, setResult] = createSignal<ReindexResult | null>(null);
  const [loading, setLoading] = createSignal(false);
  const [error, setError] = createSignal<string | null>(null);

  useKeyboard({
    d: () => {
      void triggerReindex("search", true);
    },
    s: () => {
      void triggerReindex("search", false);
    },
    v: () => {
      void triggerReindex("versions", false);
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
      <text>{"  Remote targets: search | versions"}</text>
      <text>{"  (semantic requires local CLI: nexus reindex --target semantic)"}</text>
      <text>{"  Press 'd' dry-run search, 's' reindex search, 'v' reindex versions"}</text>
      <text> </text>

      {loading() && <text>{"  Reindex in progress..."}</text>}

      {error() && <text>{`  Error: ${error()}`}</text>}

      {result() && (
        <box flexDirection="column">
          <text>{`  Last Reindex ${result()!.dryRun ? "(dry run)" : ""}`}</text>
          <text>{`  Target:     ${result()!.target}`}</text>
          <text>{`  Total:      ${result()!.total} MCL records`}</text>
          <text>{`  Processed:  ${result()!.processed}`}</text>
          <text>{`  Errors:     ${result()!.errors}`}</text>
          <text>{`  Last seq:   ${result()!.lastSequence}`}</text>
        </box>
      )}

      {!result() && !loading() && !error() && (
        <text>{"  No reindex operations performed in this session."}</text>
      )}
    </box>
  );
}
