/**
 * Locks tab: distributed lock list with acquire/release/extend actions.
 *
 * Extracted from events-panel.tsx (Issue 2A).
 */

import { createSignal, createEffect, onCleanup } from "solid-js";
import type { JSX } from "solid-js";
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

export function LocksTab(props: LocksTabProps): JSX.Element {
  const client = useApi();
  const confirm = useConfirmStore.getState().confirm;

  const [_rev, _setRev] = createSignal(0);
  const unsub = useInfraStore.subscribe(() => _setRev((r) => r + 1));
  onCleanup(unsub);
  const inf = () => { void _rev(); return useInfraStore.getState(); };

  const locks = () => inf().locks;
  const locksLoading = () => inf().locksLoading;
  const selectedLockIndex = () => inf().selectedLockIndex;
  const setSelectedLockIndex = useInfraStore.getState().setSelectedLockIndex;
  const fetchLocks = useInfraStore.getState().fetchLocks;
  const acquireLock = useInfraStore.getState().acquireLock;
  const releaseLock = useInfraStore.getState().releaseLock;
  const extendLock = useInfraStore.getState().extendLock;

  createEffect(() => {
    if (client) fetchLocks(client);
  });

  const acquireInput = useTextInput({
    onSubmit: (val) => {
      if (val && client) acquireLock(val, "mutex", 60, client);
    },
  });

  useKeyboard(
    (): Record<string, () => void> => {
      if (props.overlayActive) return {};
      if (acquireInput.active) return acquireInput.inputBindings;
      const listNav = listNavigationBindings({
        getIndex: () => selectedLockIndex(),
        setIndex: (i) => setSelectedLockIndex(i),
        getLength: () => locks().length,
      });
      return {
        ...listNav,
        ...props.tabBindings,
        n: () => acquireInput.activate(""),
        d: async () => {
          if (client) {
            const lock = locks()[selectedLockIndex()];
            if (lock) {
              const ok = await confirm("Release lock?", "Release this lock. Other waiters may acquire it.");
              if (!ok) return;
              releaseLock(lock.resource, lock.lock_id, client);
            }
          }
        },
        e: () => {
          if (client) {
            const lock = locks()[selectedLockIndex()];
            if (lock) extendLock(lock.resource, lock.lock_id, 60, client);
          }
        },
        r: () => { if (client) fetchLocks(client); },
      };
    },
    () => props.overlayActive ? undefined : acquireInput.active ? acquireInput.onUnhandled : undefined,
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
          locks={locks()}
          selectedIndex={selectedLockIndex()}
          loading={locksLoading()}
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
