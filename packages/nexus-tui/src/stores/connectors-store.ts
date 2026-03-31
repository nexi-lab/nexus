/**
 * Connector management state: discovery, auth, mount, sync, skills, writes.
 *
 * Single store following the one-store-per-panel pattern (Decision 3A).
 */

import { create } from "zustand";
import { createApiAction } from "./create-api-action.js";
import type { FetchClient } from "@nexus/api-client";
import { useUiStore } from "./ui-store.js";

// =============================================================================
// Types (wire format — snake_case matches API responses)
// =============================================================================

export interface AvailableConnector {
  readonly name: string;
  readonly description: string;
  readonly category: string;
  readonly capabilities: readonly string[];
  readonly user_scoped: boolean;
  readonly auth_status: string;
  readonly auth_source: string | null;
  readonly mount_path: string | null;
  readonly sync_status: string | null;
}

export interface MountInfo {
  readonly mount_point: string;
  readonly readonly: boolean;
  readonly connector_type: string | null;
  readonly skill_name: string | null;
  readonly operations: readonly string[];
  readonly sync_status: string | null;
  readonly last_sync: string | null;
}

export interface AuthInitResult {
  readonly auth_url: string;
  readonly state_token: string;
  readonly provider: string;
  readonly expires_in: number;
}

export interface AuthStatusResult {
  readonly status: string; // "pending" | "completed" | "denied" | "expired" | "error"
  readonly connector_name: string;
  readonly message: string | null;
}

export interface SyncResult {
  readonly mount_point: string;
  readonly files_scanned: number;
  readonly files_synced: number;
  readonly delta_added: number;
  readonly delta_deleted: number;
  readonly history_id: string | null;
  readonly is_delta: boolean;
  readonly error: string | null;
}

export interface SkillDoc {
  readonly mount_point: string;
  readonly content: string;
  readonly schemas: readonly string[];
}

export interface SchemaDoc {
  readonly mount_point: string;
  readonly operation: string;
  readonly content: string;
}

export interface WriteResult {
  readonly success: boolean;
  readonly content_hash: string | null;
  readonly error: string | null;
}

// =============================================================================
// Sub-tab type
// =============================================================================

export type ConnectorsTab = "available" | "mounted" | "skills" | "write";

// =============================================================================
// Auth flow state
// =============================================================================

export type AuthFlowStatus = "idle" | "waiting" | "polling" | "completed" | "error";

export interface AuthFlowState {
  readonly status: AuthFlowStatus;
  readonly auth_url: string | null;
  readonly state_token: string | null;
  readonly connector_name: string | null;
  readonly error_message: string | null;
}

const INITIAL_AUTH_FLOW: AuthFlowState = {
  status: "idle",
  auth_url: null,
  state_token: null,
  connector_name: null,
  error_message: null,
};

// =============================================================================
// Store interface
// =============================================================================

export interface ConnectorsState {
  // --- Shared ---
  readonly error: string | null;

  // --- Available tab ---
  readonly availableConnectors: readonly AvailableConnector[];
  readonly availableLoading: boolean;
  readonly selectedAvailableIndex: number;
  readonly authFlow: AuthFlowState;

  // --- Mounted tab ---
  readonly mounts: readonly MountInfo[];
  readonly mountsLoading: boolean;
  readonly selectedMountIndex: number;
  readonly syncingMounts: ReadonlySet<string>;
  readonly lastSyncResult: SyncResult | null;

  // --- Skills tab ---
  readonly selectedSkillMountIndex: number;
  readonly skillDoc: SkillDoc | null;
  readonly skillDocLoading: boolean;
  readonly selectedSchemaIndex: number;
  readonly schemaDoc: SchemaDoc | null;
  readonly schemaDocLoading: boolean;
  readonly skillViewMode: "doc" | "schema";

  // --- Write tab ---
  readonly selectedWriteMountIndex: number;
  readonly selectedOperationIndex: number;
  readonly writeTemplate: string;
  readonly writeResult: WriteResult | null;
  readonly writeLoading: boolean;

  // --- Navigation ---
  readonly activeTab: ConnectorsTab;

  // --- Actions ---
  readonly setActiveTab: (tab: ConnectorsTab) => void;
  readonly setSelectedAvailableIndex: (i: number) => void;
  readonly setSelectedMountIndex: (i: number) => void;
  readonly setSelectedSkillMountIndex: (i: number) => void;
  readonly setSelectedSchemaIndex: (i: number) => void;
  readonly setSkillViewMode: (mode: "doc" | "schema") => void;
  readonly setSelectedWriteMountIndex: (i: number) => void;
  readonly setSelectedOperationIndex: (i: number) => void;
  readonly setWriteTemplate: (template: string) => void;

  // --- Async actions ---
  readonly fetchAvailable: (client: FetchClient) => Promise<void>;
  readonly fetchMounts: (client: FetchClient) => Promise<void>;
  readonly initiateAuth: (connectorName: string, client: FetchClient) => Promise<void>;
  readonly pollAuthStatus: (client: FetchClient) => Promise<void>;
  readonly cancelAuth: () => void;
  readonly mountConnector: (connectorType: string, mountPoint: string, client: FetchClient) => Promise<void>;
  readonly unmountConnector: (mountPoint: string, client: FetchClient) => Promise<void>;
  readonly triggerSync: (mountPoint: string, client: FetchClient) => Promise<void>;
  readonly fetchSkillDoc: (mountPath: string, client: FetchClient) => Promise<void>;
  readonly fetchSchema: (mountPath: string, operation: string, client: FetchClient) => Promise<void>;
  readonly submitWrite: (mountPath: string, yamlContent: string, client: FetchClient) => Promise<void>;
  readonly clearWriteResult: () => void;
  readonly clearSyncResult: () => void;
}

// =============================================================================
// Store implementation
// =============================================================================

export const useConnectorsStore = create<ConnectorsState>((set, get) => ({
  // --- Initial state ---
  error: null,

  availableConnectors: [],
  availableLoading: false,
  selectedAvailableIndex: 0,
  authFlow: INITIAL_AUTH_FLOW,

  mounts: [],
  mountsLoading: false,
  selectedMountIndex: 0,
  syncingMounts: new Set<string>(),
  lastSyncResult: null,

  selectedSkillMountIndex: 0,
  skillDoc: null,
  skillDocLoading: false,
  selectedSchemaIndex: 0,
  schemaDoc: null,
  schemaDocLoading: false,
  skillViewMode: "doc" as const,

  selectedWriteMountIndex: 0,
  selectedOperationIndex: 0,
  writeTemplate: "",
  writeResult: null,
  writeLoading: false,

  activeTab: "available" as ConnectorsTab,

  // --- Setters ---
  setActiveTab: (tab) => set({ activeTab: tab }),
  setSelectedAvailableIndex: (i) => set({ selectedAvailableIndex: i }),
  setSelectedMountIndex: (i) => set({ selectedMountIndex: i }),
  setSelectedSkillMountIndex: (i) => set({ selectedSkillMountIndex: i }),
  setSelectedSchemaIndex: (i) => set({ selectedSchemaIndex: i }),
  setSkillViewMode: (mode) => set({ skillViewMode: mode }),
  setSelectedWriteMountIndex: (i) => set({ selectedWriteMountIndex: i }),
  setSelectedOperationIndex: (i) => set({ selectedOperationIndex: i }),
  setWriteTemplate: (template) => set({ writeTemplate: template }),

  // --- Fetch available connectors ---
  fetchAvailable: createApiAction<ConnectorsState, [FetchClient]>(set, {
    loadingKey: "availableLoading",
    action: async (client) => {
      const data = await client.get<AvailableConnector[]>("/api/v2/connectors/available");
      return { availableConnectors: data };
    },
    source: "connectors",
    retryable: true,
  }),

  // --- Fetch mounted connectors ---
  fetchMounts: createApiAction<ConnectorsState, [FetchClient]>(set, {
    loadingKey: "mountsLoading",
    action: async (client) => {
      const data = await client.get<MountInfo[]>("/api/v2/connectors/mounts");
      return { mounts: data };
    },
    source: "connectors",
    retryable: true,
  }),

  // --- OAuth auth flow ---
  initiateAuth: async (connectorName: string, client: FetchClient) => {
    set({
      authFlow: {
        status: "waiting",
        auth_url: null,
        state_token: null,
        connector_name: connectorName,
        error_message: null,
      },
      error: null,
    });

    try {
      const result = await client.post<AuthInitResult>("/api/v2/connectors/auth/init", {
        connector_name: connectorName,
      });

      // Try to open browser
      let browserOpened = false;
      try {
        const { exec } = await import("child_process");
        const platform = process.platform;
        const cmd = platform === "darwin"
          ? `open "${result.auth_url}"`
          : platform === "win32"
            ? `start "${result.auth_url}"`
            : `xdg-open "${result.auth_url}"`;
        exec(cmd);
        browserOpened = true;
      } catch {
        browserOpened = false;
      }

      set({
        authFlow: {
          status: browserOpened ? "polling" : "waiting",
          auth_url: result.auth_url,
          state_token: result.state_token,
          connector_name: connectorName,
          error_message: browserOpened ? null : "Could not open browser. Copy the URL and paste it in your browser.",
        },
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to initiate auth";
      set({
        authFlow: {
          status: "error",
          auth_url: null,
          state_token: null,
          connector_name: connectorName,
          error_message: message,
        },
      });
    }
  },

  pollAuthStatus: async (client: FetchClient) => {
    const { authFlow } = get();
    if (!authFlow.state_token || authFlow.status === "idle" || authFlow.status === "completed") {
      return;
    }

    try {
      const result = await client.get<AuthStatusResult>(
        `/api/v2/connectors/auth/status?state_token=${authFlow.state_token}`,
      );

      if (result.status === "completed") {
        set({
          authFlow: { ...authFlow, status: "completed", error_message: null },
        });
        // Refresh available connectors to show updated auth status
        const { fetchAvailable } = get();
        await fetchAvailable(client);
      } else if (result.status === "denied" || result.status === "expired" || result.status === "error") {
        set({
          authFlow: {
            ...authFlow,
            status: "error",
            error_message: result.message || `Auth ${result.status}`,
          },
        });
      }
      // "pending" — do nothing, keep polling
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to check auth status";
      set({
        authFlow: { ...authFlow, status: "error", error_message: message },
      });
    }
  },

  cancelAuth: () => {
    set({ authFlow: INITIAL_AUTH_FLOW });
  },

  // --- Mount/unmount ---
  mountConnector: createApiAction<ConnectorsState, [string, string, FetchClient]>(set, {
    loadingKey: "mountsLoading",
    action: async (connectorType, mountPoint, client) => {
      await client.post("/api/v2/connectors/mount", {
        connector_type: connectorType,
        mount_point: mountPoint,
      });
      // Refresh mounts and available lists
      const mounts = await client.get<MountInfo[]>("/api/v2/connectors/mounts");
      const available = await client.get<AvailableConnector[]>("/api/v2/connectors/available");
      return { mounts, availableConnectors: available };
    },
    source: "connectors",
  }),

  unmountConnector: createApiAction<ConnectorsState, [string, FetchClient]>(set, {
    loadingKey: "mountsLoading",
    action: async (mountPoint, client) => {
      await client.post("/api/v2/connectors/unmount", {
        connector_type: "",
        mount_point: mountPoint,
      });
      const mounts = await client.get<MountInfo[]>("/api/v2/connectors/mounts");
      const available = await client.get<AvailableConnector[]>("/api/v2/connectors/available");
      return { mounts, availableConnectors: available };
    },
    source: "connectors",
  }),

  // --- Sync ---
  triggerSync: async (mountPoint: string, client: FetchClient) => {
    const { syncingMounts } = get();
    const newSyncing = new Set(syncingMounts);
    newSyncing.add(mountPoint);
    set({ syncingMounts: newSyncing, lastSyncResult: null, error: null });

    try {
      const result = await client.post<SyncResult>("/api/v2/connectors/sync", {
        mount_point: mountPoint,
      });
      const updated = new Set(get().syncingMounts);
      updated.delete(mountPoint);
      set({ syncingMounts: updated, lastSyncResult: result });

      // Refresh mounts to get updated sync status
      const mounts = await client.get<MountInfo[]>("/api/v2/connectors/mounts");
      set({ mounts });
      useUiStore.getState().markDataUpdated("connectors");
    } catch (err) {
      const updated = new Set(get().syncingMounts);
      updated.delete(mountPoint);
      const message = err instanceof Error ? err.message : "Sync failed";
      set({ syncingMounts: updated, error: message });
    }
  },

  // --- Skill docs ---
  fetchSkillDoc: createApiAction<ConnectorsState, [string, FetchClient]>(set, {
    loadingKey: "skillDocLoading",
    action: async (mountPath, client) => {
      const doc = await client.get<SkillDoc>(`/api/v2/connectors/skill/${mountPath.replace(/^\//, "")}`);
      return { skillDoc: doc };
    },
    source: "connectors",
  }),

  // --- Schema ---
  fetchSchema: createApiAction<ConnectorsState, [string, string, FetchClient]>(set, {
    loadingKey: "schemaDocLoading",
    action: async (mountPath, operation, client) => {
      const doc = await client.get<SchemaDoc>(
        `/api/v2/connectors/schema/${mountPath.replace(/^\//, "")}/${operation}`,
      );
      return { schemaDoc: doc };
    },
    source: "connectors",
  }),

  // --- Write ---
  submitWrite: createApiAction<ConnectorsState, [string, string, FetchClient]>(set, {
    loadingKey: "writeLoading",
    action: async (mountPath, yamlContent, client) => {
      const result = await client.post<WriteResult>(
        `/api/v2/connectors/write/${mountPath.replace(/^\//, "")}`,
        { yaml_content: yamlContent },
      );
      return { writeResult: result };
    },
    source: "connectors",
  }),

  clearWriteResult: () => set({ writeResult: null }),
  clearSyncResult: () => set({ lastSyncResult: null }),
}));
