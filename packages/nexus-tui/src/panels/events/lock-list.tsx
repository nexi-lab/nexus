/**
 * Lock list view: shows distributed locks with holder and TTL info.
 */

import React from "react";
import type { Lock } from "../../stores/infra-store.js";
import { Spinner } from "../../shared/components/spinner.js";

const STATUS_ICON: Record<string, string> = {
  held: "🔒",
  released: "🔓",
  expired: "○",
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
        <text>{"  St  Resource                 Holder               TTL(s)  Expires"}</text>
      </box>

      {locks.map((lock, i) => {
        const prefix = i === selectedIndex ? "> " : "  ";
        const icon = STATUS_ICON[lock.status] ?? "?";
        const resource = lock.resource.padEnd(24).slice(0, 24);
        const holder = lock.holder.padEnd(20).slice(0, 20);
        const ttl = String(lock.ttl_seconds).padStart(6);
        const expires = lock.expires_at.slice(11, 19);

        return (
          <box key={lock.lock_id} height={1} width="100%">
            <text>{`${prefix}${icon} ${resource} ${holder} ${ttl}  ${expires}`}</text>
          </box>
        );
      })}
    </scrollbox>
  );
}
