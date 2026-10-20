import { FetchClient, resolveConfig, type NativeIdentity, type NexusClientOptions } from "@nexus-ai-fs/api-client";
import { createStore } from "./create-store.js";

export type PanelId = "files" | "search" | "console";
export type ConnectionStatus = "disconnected" | "connecting" | "connected" | "error";
let clientFactory = (config: NexusClientOptions): FetchClient => new FetchClient(config);
let connectionAttempt = 0;
export function setClientFactory(factory: (config: NexusClientOptions) => FetchClient) { clientFactory = factory; }

interface GlobalState {
  config: NexusClientOptions;
  client: FetchClient | null;
  connectionStatus: ConnectionStatus;
  connectionError: string | null;
  serverVersion: string | null;
  userInfo: NativeIdentity | null;
  activePanel: PanelId;
  initConfig: (options?: Partial<NexusClientOptions>) => void;
  testConnection: () => Promise<void>;
  setConnectionStatus: (status: ConnectionStatus, error?: string) => void;
  setActivePanel: (panel: PanelId) => void;
}
const record = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null;
function identity(value: unknown): value is NativeIdentity {
  return record(value) && ["subject_type", "subject_id", "user_id", "zone_id"].every((name) => typeof value[name] === "string")
    && (value.agent_id === null || typeof value.agent_id === "string") && typeof value.is_admin === "boolean"
    && Array.isArray(value.zone_perms) && value.zone_perms.every((pair: unknown) => Array.isArray(pair)
      && pair.length === 2 && pair.every((item: unknown) => typeof item === "string"));
}
export const useGlobalStore = createStore<GlobalState>((set, get) => ({
  config: { apiKey: "" }, client: null, connectionStatus: "disconnected", connectionError: null,
  serverVersion: null, userInfo: null, activePanel: "files",
  initConfig: (options) => {
    ++connectionAttempt;
    const config = resolveConfig({ ...options, maxRetries: options?.maxRetries ?? 0, transformKeys: false });
    set({ config, client: config.apiKey ? clientFactory(config) : null, connectionStatus: "disconnected",
      connectionError: null, serverVersion: null, userInfo: null, activePanel: "files" });
    if (config.apiKey) void get().testConnection();
  },
  testConnection: async () => {
    const { client, config } = get();
    const attempt = ++connectionAttempt;
    const current = () => attempt === connectionAttempt && get().client === client && get().config === config;
    if (!client) { set({ connectionStatus: "disconnected", userInfo: null }); return; }
    set({ connectionStatus: "connecting", connectionError: null, userInfo: null });
    try {
      const [status, principal] = await Promise.all([
        client.get<unknown>("/v2/status", { timeout: 8000, transformKeys: false }),
        client.get<unknown>("/v2/auth/whoami", { timeout: 8000, transformKeys: false }),
      ]);
      if (!current()) return;
      if (!record(status) || status.status !== "ok" || typeof status.version !== "string" || !identity(principal))
        throw new Error("Invalid native connection response");
      set({ connectionStatus: "connected", serverVersion: status.version, userInfo: principal });
    } catch (error) {
      if (!current()) return;
      set({ connectionStatus: "error", connectionError: error instanceof Error ? error.message : String(error), userInfo: null });
    }
  },
  setConnectionStatus: (status, error) => {
    ++connectionAttempt;
    set({ connectionStatus: status, connectionError: error ?? null, ...(status !== "connected" ? { userInfo: null } : {}) });
  },
  setActivePanel: (panel) => set({ activePanel: panel }),
}));
