/**
 * Delegation chain view: read-only overlay showing the delegation
 * hierarchy from root to leaf, fetched via the chain endpoint.
 *
 * Escape closes.
 */

import React, { useEffect } from "react";
import { useAccessStore } from "../../stores/access-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";

interface DelegationChainViewProps {
  readonly delegationId: string;
  readonly onClose: () => void;
}

export function DelegationChainView({
  delegationId,
  onClose,
}: DelegationChainViewProps): React.ReactNode {
  const client = useApi();
  const chain = useAccessStore((s) => s.delegationChain);
  const loading = useAccessStore((s) => s.delegationChainLoading);
  const error = useAccessStore((s) => s.error);
  const fetchDelegationChain = useAccessStore((s) => s.fetchDelegationChain);

  useEffect(() => {
    if (client) {
      fetchDelegationChain(delegationId, client);
    }
  }, [delegationId, client, fetchDelegationChain]);

  useKeyboard({ escape: onClose });

  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading delegation chain...</text>
      </box>
    );
  }

  if (error) {
    return (
      <box height="100%" width="100%" flexDirection="column">
        <box height={1} width="100%">
          <text>{`Error: ${error}`}</text>
        </box>
        <box height={1} width="100%">
          <text>{"Escape:close"}</text>
        </box>
      </box>
    );
  }

  if (!chain || chain.chain.length === 0) {
    return (
      <box height="100%" width="100%" flexDirection="column">
        <box height={1} width="100%">
          <text>{`No chain found for delegation ${delegationId}`}</text>
        </box>
        <box height={1} width="100%">
          <text>{"Escape:close"}</text>
        </box>
      </box>
    );
  }

  return (
    <box height="100%" width="100%" flexDirection="column">
      <box height={1} width="100%">
        <text>{`Delegation Chain (depth: ${chain.total_depth})`}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  DEPTH  AGENT            PARENT           MODE       STATUS     INTENT"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  -----  ---------------  ---------------  ---------  ---------  ----------"}</text>
      </box>

      <scrollbox flexGrow={1} width="100%">
        {chain.chain.map((entry) => {
          const indent = "  ".repeat(entry.depth);
          const agent = entry.agent_id.length > 15
            ? `${entry.agent_id.slice(0, 12)}..`
            : entry.agent_id;
          const parent = entry.parent_agent_id.length > 15
            ? `${entry.parent_agent_id.slice(0, 12)}..`
            : entry.parent_agent_id;
          const intentStr = entry.intent.length > 20
            ? `${entry.intent.slice(0, 17)}...`
            : entry.intent;

          return (
            <box key={entry.delegation_id} height={1} width="100%">
              <text>
                {`${indent}  ${String(entry.depth).padEnd(5)}  ${agent.padEnd(15)}  ${parent.padEnd(15)}  ${entry.delegation_mode.padEnd(9)}  ${entry.status.padEnd(9)}  ${intentStr}`}
              </text>
            </box>
          );
        })}
      </scrollbox>

      <box height={1} width="100%">
        <text>{"Escape:close"}</text>
      </box>
    </box>
  );
}
