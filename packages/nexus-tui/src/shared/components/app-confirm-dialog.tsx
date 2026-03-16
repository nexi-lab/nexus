/**
 * App-level ConfirmDialog that reads from the imperative useConfirmStore.
 *
 * Mounted once in App — panels call `useConfirmStore.getState().confirm()`
 * to show the dialog imperatively.
 *
 * @see Issue #3066 Architecture Decision 3A
 */

import React from "react";
import { useConfirmStore } from "../hooks/use-confirm.js";
import { ConfirmDialog } from "./confirm-dialog.js";

export function AppConfirmDialog(): React.ReactNode {
  const visible = useConfirmStore((s) => s.visible);
  const title = useConfirmStore((s) => s.title);
  const message = useConfirmStore((s) => s.message);
  const resolve = useConfirmStore((s) => s.resolve);

  return (
    <ConfirmDialog
      visible={visible}
      title={title}
      message={message}
      onConfirm={() => resolve?.(true)}
      onCancel={() => resolve?.(false)}
    />
  );
}
