/**
 * Locks tab: distributed lock list with acquire/release/extend actions.
 *
 * Extracted from events-panel.tsx (Issue 2A).
 */

import React, { useEffect } from "react";
import { useInfraStore } from "../../stores/infra-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { listNavigationBindings } from "../../shared/hooks/use-list-navigation.js";
import { useTextInput } from "../../shared/hooks/use-text-input.js";
import { useConfirmStore } from "../../shared/hooks/use-confirm.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { LockList } from "./lock-list.js";

interface LocksTabProps {
  readonly tabBindings: Readonly<Record<string, () => void>>;
  readonly overlayActive: boolean;
}

export function LocksTab({ tabBindings, overlayActive }: LocksTabProps): React.ReactNode {
  const client = useApi();
  const confirm = useConfirmStore((s) => s.confirm);

  const locks = useInfraStore((s) => s.locks);
  const locksLoading = useInfraStore((s) => s.locksLoading);
  const selectedLockIndex = useInfraStore((s) => s.selectedLockIndex);
  const setSelectedLockIndex = useInfraStore((s) => s.setSelectedLockIndex);
  const fetchLocks = useInfraStore((s) => s.fetchLocks);
  const acquireLock = useInfraStore((s) => s.acquireLock);
  const releaseLock = useInfraStore((s) => s.releaseLock);
  const extendLock = useInfraStore((s) => s.extendLock);

  useEffect(() => {
    if (client) fetchLocks(client);
  }, [client, fetchLocks]);

  const acquireInput = useTextInput({
    onSubmit: (val) => {
      if (val && client) acquireLock(val, "mutex", 60, client);
    },
  });

  const listNav = listNavigationBindings({
    getIndex: () => selectedLockIndex,
    setIndex: (i) => setSelectedLockIndex(i),
    getLength: () => locks.length,
  });

  useKeyboard(
    overlayActive
      ? {}
      : acquireInput.active
      ? acquireInput.inputBindings
      : {
          ...listNav,
          ...tabBindings,
          n: () => acquireInput.activate(""),
          d: async () => {
            if (client) {
              const lock = locks[selectedLockIndex];
              if (lock) {
                const ok = await confirm("Release lock?", "Release this lock. Other waiters may acquire it.");
                if (!ok) return;
                releaseLock(lock.resource, lock.lock_id, client);
              }
            }
          },
          e: () => {
            if (client) {
              const lock = locks[selectedLockIndex];
              if (lock) extendLock(lock.resource, lock.lock_id, 60, client);
            }
          },
          r: () => { if (client) fetchLocks(client); },
        },
    overlayActive ? undefined : acquireInput.active ? acquireInput.onUnhandled : undefined,
  );

  return (
    <box height="100%" width="100%" flexDirection="column">
      {acquireInput.active && (
        <box height={1} width="100%">
          <text>{`Acquire lock path: ${acquireInput.buffer}\u2588`}</text>
        </box>
      )}
      <box flexGrow={1} width="100%" borderStyle="single">
        <LockList
          locks={locks}
          selectedIndex={selectedLockIndex}
          loading={locksLoading}
        />
      </box>
      <box height={1} width="100%">
        <text>
          {acquireInput.active
            ? "Type path, Enter:acquire, Escape:cancel"
            : "j/k:navigate  n:acquire  d:release  e:extend  r:refresh  Tab:switch tab"}
        </text>
      </box>
    </box>
  );
}
