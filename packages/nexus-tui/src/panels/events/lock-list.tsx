import { For } from "solid-js";
import type { JSX } from "solid-js";
/**
 * Lock list view: shows distributed locks with holder and TTL info.
 */

import type { Lock } from "../../stores/infra-store.js";

const MODE_ICON: Record<string, string> = {
  mutex: "🔒",
  semaphore: "🔗",
};

export function LockList(props: {
  readonly locks: readonly Lock[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}): JSX.Element {
  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>
        {props.loading
          ? "Loading locks..."
          : props.locks.length === 0
            ? "No active locks"
            : `${props.locks.length} locks`}
      </text>
      <scrollbox flexGrow={1} width="100%">
        {/* Header */}
        <box height={1} width="100%">
          <text>{"  Mode  Resource                 Holder               Fence   Expires"}</text>
        </box>

        <For each={props.locks}>{(lock, i) => {
          const prefix = () => i() === props.selectedIndex ? "> " : "  ";
          const icon = MODE_ICON[lock.mode] ?? "?";
          const resource = lock.resource.padEnd(24).slice(0, 24);
          const holder = lock.holder_info.padEnd(20).slice(0, 20);
          const fence = String(lock.fence_token).padStart(6);
          const expires = new Date(lock.expires_at * 1000).toISOString().slice(11, 19);

          return (
            <box height={1} width="100%">
              <text>{`${prefix()}${icon} ${resource} ${holder} ${fence}  ${expires}`}</text>
            </box>
          );
        }}</For>
      </scrollbox>
    </box>
  );
}
