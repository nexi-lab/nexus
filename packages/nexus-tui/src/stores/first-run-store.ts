/**
 * Zustand store tracking dismissed first-run tooltips.
 *
 * Persists in memory only (resets on restart) which is acceptable for
 * lightweight "Tip: Press ? for help" style tooltips.
 */

import { create } from "zustand";

export interface FirstRunState {
  /** Set of tooltip keys that have been dismissed. */
  readonly dismissed: ReadonlySet<string>;

  /** Whether a tooltip for the given key should be shown. */
  readonly shouldShow: (key: string) => boolean;

  /** Mark a tooltip as dismissed. */
  readonly dismiss: (key: string) => void;
}

export const useFirstRunStore = create<FirstRunState>((set, get) => ({
  dismissed: new Set(),

  shouldShow: (key) => !get().dismissed.has(key),

  dismiss: (key) => {
    set((state) => {
      if (state.dismissed.has(key)) return state;
      const next = new Set(state.dismissed);
      next.add(key);
      return { dismissed: next };
    });
  },
}));
