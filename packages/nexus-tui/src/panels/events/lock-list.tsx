/**
 * Lock list view: shows distributed locks with holder and TTL info.
 */

import React from "react";
import type { Lock } from "../../stores/infra-store.js";
import { Spinner } from "../../shared/components/spinner.js";

const MODE_ICON: Record<string, string> = {
  mutex: "🔒",
  semaphore: "🔗",
};

export function LockList({
  locks,
  selectedIndex,
  loading,
}: {
  readonly locks: readonly Lock[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}): React.ReactNode {
  if (loading) {
    return <Spinner label="Loading locks..." />;
  }

  if (locks.length === 0) {
    return <text>No active locks</text>;
  }

  return (
    <scrollbox height="100%" width="100%">
      {/* Header */}
      <box height={1} width="100%">
        <text>{"  Mode  Resource                 Holder               Fence   Expires"}</text>
      </box>

      {locks.map((lock, i) => {
        const prefix = i === selectedIndex ? "> " : "  ";
        const icon = MODE_ICON[lock.mode] ?? "?";
        const resource = lock.resource.padEnd(24).slice(0, 24);
        const holder = lock.holder_info.padEnd(20).slice(0, 20);
        const fence = String(lock.fence_token).padStart(6);
        const expires = new Date(lock.expires_at * 1000).toISOString().slice(11, 19);

        return (
          <box key={lock.lock_id} height={1} width="100%">
            <text>{`${prefix}${icon} ${resource} ${holder} ${fence}  ${expires}`}</text>
          </box>
        );
      })}
    </scrollbox>
  );
}
