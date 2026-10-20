import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import { FetchClient } from "@nexus-ai-fs/api-client";
import { useGlobalStore, setClientFactory } from "../../src/stores/global-store.js";

const principal = { subject_type: "user", subject_id: "alice", user_id: "alice", agent_id: null,
  zone_id: "shared", is_admin: false, zone_perms: [["shared", "r"]] };
describe("authenticated native connection", () => {
  beforeEach(() => useGlobalStore.setState({ client: null, connectionStatus: "disconnected", userInfo: null,
    connectionError: null, activePanel: "files", serverVersion: null, config: { apiKey: "fixture", baseUrl: "http://127.0.0.1:1" } }));
  afterEach(() => { useGlobalStore.getState().setConnectionStatus("disconnected"); setClientFactory((config) => new FetchClient(config)); });
  it("requires both native liveness and the credential's verified identity", async () => {
    const routes: string[] = [];
    const client = new FetchClient({ apiKey: "fixture", baseUrl: "http://127.0.0.1:1", maxRetries: 0,
      fetch: (async (url) => {
        routes.push(new URL(String(url)).pathname);
        return Response.json(String(url).endsWith("/v2/status") ? { status: "ok", version: "1" } : principal);
      }) as typeof fetch });
    useGlobalStore.setState({ client });
    await useGlobalStore.getState().testConnection();
    expect(useGlobalStore.getState().connectionStatus).toBe("connected");
    expect(useGlobalStore.getState().userInfo).toEqual(principal);
    expect(routes.sort()).toEqual(["/v2/auth/whoami", "/v2/status"]);
  });
  it("a reachable public status route cannot authenticate a revoked key", async () => {
    const client = new FetchClient({ apiKey: "revoked", baseUrl: "http://127.0.0.1:1", maxRetries: 0,
      fetch: (async (url) => String(url).endsWith("/v2/status") ? Response.json({ status: "ok", version: "1" })
        : Response.json({ error: "invalid credentials" }, { status: 401 })) as typeof fetch });
    useGlobalStore.setState({ client });
    await useGlobalStore.getState().testConnection();
    expect(useGlobalStore.getState().connectionStatus).toBe("error");
    expect(useGlobalStore.getState().connectionError).toContain("invalid credentials");
    expect(useGlobalStore.getState().userInfo).toBeNull();
  });
  it("rejects incomplete identity responses", async () => {
    const client = { get: async (path: string) => path.endsWith("status") ? { status: "ok", version: "1" } : { is_admin: true } } as FetchClient;
    useGlobalStore.setState({ client });
    await useGlobalStore.getState().testConnection();
    expect(useGlobalStore.getState().connectionStatus).toBe("error");
    expect(useGlobalStore.getState().userInfo).toBeNull();
  });
  it("supports only the current native panels and clears identity on disconnect", () => {
    useGlobalStore.getState().setActivePanel("search");
    expect(useGlobalStore.getState().activePanel).toBe("search");
    useGlobalStore.setState({ userInfo: principal });
    useGlobalStore.getState().setConnectionStatus("disconnected");
    expect(useGlobalStore.getState().userInfo).toBeNull();
  });
});
