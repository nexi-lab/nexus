import type { PanelId } from "../stores/global-store.js";

export interface KeyBinding {
  readonly key: string;
  readonly action: string;
}

export function formatActionHints(bindings: readonly KeyBinding[]): string {
  return bindings
    .map((binding) => binding.action ? `${binding.key}:${binding.action}` : binding.key)
    .join("  ");
}

export const GLOBAL_BINDINGS: readonly KeyBinding[] = [
  { key: "Ctrl+P", action: "Command palette" },
  { key: "1-9,0", action: "Switch panel" },
  { key: "Ctrl+I", action: "Identity switcher" },
  { key: "Ctrl+D", action: "Disconnect" },
  { key: "z", action: "Toggle zoom" },
  { key: "?", action: "Help overlay" },
  { key: "q", action: "Quit" },
];

export const NAV_BINDINGS: readonly KeyBinding[] = [
  { key: "j/↓", action: "Move down" },
  { key: "k/↑", action: "Move up" },
  { key: "g", action: "Jump to top" },
  { key: "G", action: "Jump to bottom" },
  { key: "Enter", action: "Select/expand" },
  { key: "Tab", action: "Switch pane/tab" },
  { key: "Esc", action: "Cancel/back" },
];

export const PANEL_BINDINGS: Record<PanelId, readonly KeyBinding[]> = {
  files: [
    { key: "l/→", action: "Expand folder" },
    { key: "h/←", action: "Collapse folder" },
    { key: "d", action: "Delete file" },
    { key: "Shift+N", action: "New directory" },
    { key: "Shift+R", action: "Rename" },
    { key: "e", action: "Edit file" },
    { key: "Shift+E", action: "Create new file" },
    { key: "/", action: "Quick filter" },
    { key: "Ctrl+F", action: "Power search" },
    { key: "v", action: "Toggle visual mode" },
    { key: "Space", action: "Toggle select file" },
    { key: "c", action: "Copy selected" },
    { key: "x", action: "Cut selected" },
    { key: "p", action: "Paste clipboard here" },
    { key: "Shift+P", action: "Paste to path" },
    { key: "Esc", action: "Clear selection / exit mode" },
  ],
  versions: [
    { key: "n", action: "New transaction" },
    { key: "Enter", action: "Commit transaction" },
    { key: "Backspace", action: "Rollback" },
    { key: "v", action: "View diff" },
    { key: "c", action: "Toggle conflicts" },
    { key: "f", action: "Cycle status filter" },
  ],
  agents: [
    { key: "d", action: "Revoke delegation" },
    { key: "r", action: "Refresh" },
    { key: "Shift+W", action: "Warmup agent" },
    { key: "Shift+E", action: "Evict agent" },
    { key: "Shift+V", action: "Verify agent" },
  ],
  zones: [
    { key: "n", action: "Register new" },
    { key: "d", action: "Unregister" },
    { key: "m", action: "Mount brick" },
    { key: "u", action: "Unmount brick" },
    { key: "x", action: "Reset brick" },
    { key: "r", action: "Remount" },
  ],
  access: [
    { key: "n", action: "New delegation" },
    { key: "Shift+X", action: "Revoke manifest" },
    { key: "x", action: "Revoke credential" },
    { key: "s", action: "Suspend agent" },
    { key: "o", action: "Complete delegation" },
    { key: "v", action: "View chain" },
    { key: "p", action: "Permission check" },
    { key: "f", action: "Cycle filter" },
  ],
  payments: [
    { key: "n", action: "New policy" },
    { key: "d", action: "Delete policy" },
    { key: "t", action: "Transfer funds" },
    { key: "c", action: "Commit reservation" },
    { key: "x", action: "Release reservation" },
    { key: "a", action: "Affordability check" },
    { key: "i", action: "Integrity check" },
    { key: "]", action: "Next page" },
    { key: "[", action: "Previous page" },
  ],
  search: [
    { key: "/", action: "Search" },
    { key: "m", action: "Cycle mode" },
    { key: "n", action: "Create memory" },
    { key: "u", action: "Update memory" },
    { key: "d", action: "Delete" },
    { key: "v", action: "View diff" },
  ],
  workflows: [
    { key: "e", action: "Execute workflow" },
    { key: "d", action: "Delete workflow" },
    { key: "p", action: "Toggle enabled" },
  ],
  infrastructure: [
    { key: "d", action: "Delete subscription" },
    { key: "t", action: "Test subscription" },
    { key: "r", action: "Reconnect SSE" },
    { key: "c", action: "Clear events" },
    { key: "f", action: "Filter by type" },
    { key: "s", action: "Search filter" },
  ],
  console: [
    { key: ":", action: "Command mode" },
    { key: "Enter", action: "Execute request" },
    { key: "/", action: "Filter endpoints" },
  ],
  connectors: [
    { key: "r", action: "Refresh" },
    { key: "Tab", action: "Switch tab" },
  ],
  stack: [
    { key: "r", action: "Refresh" },
    { key: "Tab", action: "Switch tab" },
  ],
};

interface ClipboardSummary {
  readonly paths: readonly string[];
  readonly operation: "copy" | "cut";
}

export function getFilesFooterBindings({
  inputMode,
  activeTab,
  catalogAvailable,
  visualMode,
  selectionCount,
  clipboard,
}: {
  readonly inputMode: "none" | "mkdir" | "rename" | "filter" | "search" | "paste-dest" | "create";
  readonly activeTab: "explorer" | "shareLinks" | "uploads";
  readonly catalogAvailable: boolean;
  readonly visualMode: boolean;
  readonly selectionCount: number;
  readonly clipboard: ClipboardSummary | null;
}): readonly KeyBinding[] {
  if (inputMode === "filter") {
    return [
      { key: "Type", action: "filter" },
      { key: "Enter", action: "keep filter" },
      { key: "Escape", action: "clear" },
    ];
  }
  if (inputMode === "search") {
    return [
      { key: "g", action: "pattern=glob" },
      { key: "r", action: "pattern=grep" },
      { key: "plain", action: "deep search" },
      { key: "Enter", action: "search" },
      { key: "Esc", action: "cancel" },
    ];
  }
  if (inputMode === "paste-dest") {
    return [
      { key: "Enter path", action: "" },
      { key: "Enter", action: "paste" },
      { key: "Escape", action: "cancel" },
    ];
  }
  if (inputMode !== "none") {
    return [
      { key: "Type name", action: "" },
      { key: "Enter", action: "confirm" },
      { key: "Escape", action: "cancel" },
      { key: "Backspace", action: "delete" },
    ];
  }

  if (activeTab === "explorer") {
    const bindings: KeyBinding[] = [
      { key: "j/k", action: "nav" },
      { key: "l/Enter", action: "expand" },
      { key: "h", action: "collapse" },
    ];

    if (visualMode) {
      bindings.push(
        { key: "v", action: "exit visual" },
        { key: "c", action: "copy" },
        { key: "x", action: "cut" },
      );
    } else if (selectionCount > 0) {
      bindings.push(
        { key: `${selectionCount} selected`, action: "" },
        { key: "c", action: "copy" },
        { key: "x", action: "cut" },
        { key: "Esc", action: "clear" },
      );
    } else {
      bindings.push(
        { key: "/", action: "filter" },
        { key: "Ctrl+F", action: "search" },
        { key: "v", action: "visual" },
        { key: "Space", action: "select" },
      );
    }

    if (clipboard) {
      bindings.push(
        {
          key: "p",
          action: `paste ${clipboard.paths.length} ${clipboard.operation === "cut" ? "cut" : "copied"}`,
        },
        { key: "P", action: "paste to path" },
      );
    }

    bindings.push(
      { key: "d", action: "del" },
      { key: "N", action: "mkdir" },
      { key: "R", action: "rename" },
      { key: "e", action: "edit" },
      { key: "E", action: "new file" },
    );

    if (catalogAvailable) {
      bindings.push({ key: "m/a/s", action: "meta" });
    }

    bindings.push({ key: "?", action: "help" });
    return bindings;
  }

  if (activeTab === "shareLinks") {
    return [
      { key: "j/k", action: "navigate" },
      { key: "x", action: "revoke" },
      { key: "r", action: "refresh" },
      { key: "Tab", action: "switch tab" },
      { key: "q", action: "quit" },
    ];
  }

  return [
    { key: "j/k", action: "navigate" },
    { key: "Tab", action: "switch tab" },
    { key: "q", action: "quit" },
  ];
}

export function getEventsFooterBindings({
  filterMode,
  activeTab,
  connectorDetailView,
}: {
  readonly filterMode: "none" | "type" | "search" | "mcl_urn" | "mcl_aspect" | "acquire_path" | "secrets_filter" | "replay_filter";
  readonly activeTab: "events" | "mcl" | "replay" | "operations" | "audit" | "connectors" | "subscriptions" | "locks" | "secrets";
  readonly connectorDetailView: boolean;
}): readonly KeyBinding[] {
  if (filterMode !== "none") {
    return [
      { key: "Type value", action: "" },
      { key: "Enter", action: "apply" },
      { key: "Escape", action: "cancel" },
      { key: "Backspace", action: "delete" },
    ];
  }

  switch (activeTab) {
    case "events":
      return [
        { key: "j/k", action: "navigate" },
        { key: "Enter", action: "expand" },
        { key: "f", action: "filter type" },
        { key: "s", action: "search" },
        { key: "c", action: "clear" },
        { key: "r", action: "reconnect" },
        { key: "y", action: "copy" },
        { key: "Tab", action: "switch" },
      ];
    case "mcl":
      return [
        { key: "u", action: "filter URN" },
        { key: "n", action: "filter aspect" },
        { key: "r", action: "refresh" },
        { key: "Tab", action: "switch tab" },
      ];
    case "replay":
      return [
        { key: "f", action: "filter event type" },
        { key: "r", action: "refresh" },
        { key: "Tab", action: "switch tab" },
      ];
    case "connectors":
      return connectorDetailView
        ? [
            { key: "Escape", action: "back" },
            { key: "r", action: "refresh" },
            { key: "Tab", action: "switch tab" },
          ]
        : [
            { key: "j/k", action: "navigate" },
            { key: "Enter", action: "capabilities" },
            { key: "r", action: "refresh" },
            { key: "Tab", action: "switch tab" },
          ];
    case "subscriptions":
      return [
        { key: "j/k", action: "navigate" },
        { key: "d", action: "delete" },
        { key: "t", action: "test" },
        { key: "r", action: "refresh" },
        { key: "Tab", action: "switch tab" },
      ];
    case "locks":
      return [
        { key: "j/k", action: "navigate" },
        { key: "n", action: "acquire" },
        { key: "d", action: "release" },
        { key: "e", action: "extend" },
        { key: "r", action: "refresh" },
        { key: "Tab", action: "switch tab" },
      ];
    case "secrets":
      return [
        { key: "/", action: "filter" },
        { key: "r", action: "refresh" },
        { key: "Tab", action: "switch tab" },
      ];
    case "audit":
      return [
        { key: "j/k", action: "navigate" },
        { key: "m", action: "load more" },
        { key: "r", action: "refresh" },
        { key: "Tab", action: "switch tab" },
      ];
    default:
      return [
        { key: "j/k", action: "navigate" },
        { key: "r", action: "refresh" },
        { key: "Tab", action: "switch tab" },
      ];
  }
}

export function getPaymentsFooterBindings({
  showTransfer,
  activeTab,
}: {
  readonly showTransfer: boolean;
  readonly activeTab: "balance" | "reservations" | "transactions" | "policies" | "approvals";
}): readonly KeyBinding[] {
  if (showTransfer) {
    return [
      { key: "Tab", action: "next field" },
      { key: "Enter", action: "submit" },
      { key: "Escape", action: "cancel" },
    ];
  }

  switch (activeTab) {
    case "transactions":
      return [
        { key: "j/k", action: "navigate" },
        { key: "]", action: "next" },
        { key: "[", action: "prev" },
        { key: "i", action: "verify integrity" },
        { key: "y", action: "copy" },
        { key: "Tab", action: "switch tab" },
        { key: "r", action: "refresh" },
      ];
    case "policies":
      return [
        { key: "j/k", action: "navigate" },
        { key: "Tab", action: "switch tab" },
        { key: "Shift+N", action: "new" },
        { key: "d", action: "delete" },
        { key: "b", action: "budget" },
        { key: "r", action: "refresh" },
        { key: "q", action: "quit" },
      ];
    case "balance":
      return [
        { key: "Tab", action: "switch tab" },
        { key: "t", action: "transfer" },
        { key: "a", action: "afford check" },
        { key: "r", action: "refresh" },
        { key: "q", action: "quit" },
      ];
    case "approvals":
      return [
        { key: "j/k", action: "navigate" },
        { key: "n", action: "new request" },
        { key: "a", action: "approve" },
        { key: "x", action: "reject" },
        { key: "Tab", action: "switch tab" },
        { key: "r", action: "refresh" },
        { key: "q", action: "quit" },
      ];
    default:
      return [
        { key: "j/k", action: "navigate" },
        { key: "Tab", action: "switch tab" },
        { key: "t", action: "transfer" },
        { key: "r", action: "refresh" },
        { key: "c", action: "commit" },
        { key: "x", action: "release" },
        { key: "q", action: "quit" },
      ];
  }
}

export function getVersionsFooterBindings({
  txnFilterMode,
}: {
  readonly txnFilterMode: boolean;
}): readonly KeyBinding[] {
  if (txnFilterMode) {
    return [
      { key: "Type", action: "filter" },
      { key: "Enter", action: "apply" },
      { key: "Escape", action: "clear" },
    ];
  }

  return [
    { key: "j/k", action: "navigate" },
    { key: "n", action: "new txn" },
    { key: "Enter", action: "commit" },
    { key: "Backspace", action: "rollback" },
    { key: "f", action: "filter" },
    { key: "/", action: "search" },
    { key: "v", action: "diff" },
    { key: "c", action: "conflicts" },
    { key: "y", action: "copy" },
    { key: "q", action: "quit" },
  ];
}

export function getWorkflowsFooterBindings({
  activeTab,
  selectedExecution,
}: {
  readonly activeTab: "workflows" | "executions" | "scheduler";
  readonly selectedExecution: boolean;
}): readonly KeyBinding[] {
  if (activeTab === "executions" && selectedExecution) {
    return [
      { key: "j/k", action: "navigate" },
      { key: "Tab", action: "switch tab" },
      { key: "Enter", action: "detail" },
      { key: "Esc", action: "close" },
      { key: "r", action: "refresh" },
      { key: "q", action: "quit" },
    ];
  }

  return [
    { key: "j/k", action: "navigate" },
    { key: "Tab", action: "switch tab" },
    { key: "e", action: "execute" },
    { key: "d", action: "delete" },
    { key: "p", action: "enable/disable" },
    { key: "r", action: "refresh" },
    { key: "Enter", action: "detail" },
    { key: "q", action: "quit" },
  ];
}
