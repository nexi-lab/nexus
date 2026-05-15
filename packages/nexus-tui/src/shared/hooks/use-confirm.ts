/**
 * Imperative confirmation dialog system.
 *
 * Usage in any component:
 * ```ts
 * const confirm = useConfirmStore((s) => s.confirm);
 * const ok = await confirm("Delete file?", "This cannot be undone.");
 * if (ok) { doDelete(); }
 * ```
 *
 * A single <ConfirmDialog> instance at the App level reads from this store.
 *
 * @see Issue #3066 Architecture Decision 3A
 */

import { createStore } from "../../stores/create-store.js";

// =============================================================================
// Types
// =============================================================================

export interface ConfirmState {
  readonly visible: boolean;
  readonly title: string;
  readonly message: string;
  readonly resolve: ((value: boolean) => void) | null;

  /**
   * Show a confirmation dialog and wait for the user's response.
   * Returns true if confirmed, false if cancelled.
   *
   * If a previous confirmation is pending, it is auto-rejected (returns false).
   */
  readonly confirm: (title: string, message: string) => Promise<boolean>;
}

// =============================================================================
// Store
// =============================================================================

export const useConfirmStore = createStore<ConfirmState>((set, get) => ({
  visible: false,
  title: "",
  message: "",
  resolve: null,

  confirm: (title, message) => {
    // Reject any pending confirmation
    const prev = get().resolve;
    if (prev) {
      prev(false);
    }

    return new Promise<boolean>((resolve) => {
      set({
        visible: true,
        title,
        message,
        resolve: (value: boolean) => {
          resolve(value);
          set({ visible: false, title: "", message: "", resolve: null });
        },
      });
    });
  },
}));
