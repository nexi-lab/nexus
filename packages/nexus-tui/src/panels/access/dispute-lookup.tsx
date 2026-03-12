/**
 * Dispute lookup form: fetch a dispute by its ID.
 *
 * Enter fetches the dispute via the store's fetchDispute().
 * Escape cancels and returns to normal mode.
 */

import React, { useState, useCallback } from "react";
import { useAccessStore } from "../../stores/access-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";

interface DisputeLookupProps {
  readonly onClose: () => void;
}

export function DisputeLookup({ onClose }: DisputeLookupProps): React.ReactNode {
  const client = useApi();
  const fetchDispute = useAccessStore((s) => s.fetchDispute);
  const disputesLoading = useAccessStore((s) => s.disputesLoading);
  const error = useAccessStore((s) => s.error);

  const [disputeId, setDisputeId] = useState("");

  const handleSubmit = useCallback(() => {
    if (!client || !disputeId.trim()) return;
    fetchDispute(disputeId.trim(), client);
  }, [client, disputeId, fetchDispute]);

  const handleUnhandledKey = useCallback(
    (keyName: string) => {
      if (keyName.length === 1) {
        setDisputeId((b) => b + keyName);
      } else if (keyName === "space") {
        setDisputeId((b) => b + " ");
      }
    },
    [],
  );

  useKeyboard(
    {
      return: handleSubmit,
      escape: onClose,
      backspace: () => {
        setDisputeId((b) => b.slice(0, -1));
      },
    },
    handleUnhandledKey,
  );

  return (
    <box height="100%" width="100%" flexDirection="column">
      <box height={1} width="100%">
        <text>{`> Dispute ID: ${disputeId}\u2588`}</text>
      </box>

      {disputesLoading && (
        <box height={1} width="100%">
          <text>Fetching dispute...</text>
        </box>
      )}

      {error && !disputesLoading && (
        <box height={1} width="100%">
          <text>{`Error: ${error}`}</text>
        </box>
      )}

      <box height={1} width="100%">
        <text>
          {"Enter:fetch  Escape:cancel  Backspace:delete"}
        </text>
      </box>
    </box>
  );
}
