/**
 * Operations tab: simple list of operations.
 *
 * Displays operation_id, agent_id, type, status, and started_at.
 */

import React from "react";
import type { OperationItem } from "../../stores/infra-store.js";

interface OperationsTabProps {
  readonly operations: readonly OperationItem[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

export function OperationsTab({ operations, selectedIndex, loading }: OperationsTabProps): React.ReactNode {
  if (loading) return <text>Loading operations...</text>;
  if (operations.length === 0) return <text>No operations found.</text>;

  return (
    <scrollbox height="100%" width="100%">
      {/* Header */}
      <box height={1} width="100%">
        <text>{"  OPERATION_ID       AGENT_ID         TYPE        STATUS      STARTED"}</text>
      </box>

      {operations.map((op, i) => {
        const isSelected = i === selectedIndex;
        const prefix = isSelected ? "> " : "  ";
        const opShort = op.operation_id.slice(0, 16) + "...";
        const agentShort = op.agent_id ? op.agent_id.slice(0, 14) : "n/a";
        const started = op.started_at ? op.started_at.slice(0, 19) : "n/a";
        return (
          <box key={op.operation_id} height={1} width="100%">
            <text>{`${prefix}${opShort}  ${agentShort.padEnd(16)}  ${op.type.padEnd(10)}  ${op.status.padEnd(10)}  ${started}`}</text>
          </box>
        );
      })}
    </scrollbox>
  );
}
