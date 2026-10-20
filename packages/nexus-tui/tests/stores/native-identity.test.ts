import { beforeEach, describe, expect, it } from "bun:test";
import type { FetchClient, NativeIdentity } from "@nexus-ai-fs/api-client";
import { useGlobalStore } from "../../src/stores/global-store.js";
const status = { status: "ok", version: "1" };
const alice: NativeIdentity = { subject_type: "user", subject_id: "alice", user_id: "alice", agent_id: null,
  zone_id: "shared", is_admin: false, zone_perms: [["shared", "r"]] };
const bob: NativeIdentity = { ...alice, subject_id: "bob", user_id: "bob" };
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done; }); return { promise, resolve }; }
describe("native identity request lifetime", () => {
  beforeEach(() => useGlobalStore.setState({ client: null, connectionStatus: "disconnected", connectionError: null,
    userInfo: null, config: { apiKey: "fixture", baseUrl: "http://127.0.0.1:1" } }));
  it("connection readiness waits for the verified identity", async () => {
    const pending = deferred<NativeIdentity>();
    const client = { get: async (path: string) => path.endsWith("status") ? status : pending.promise } as FetchClient;
    useGlobalStore.setState({ client });
    const connection = useGlobalStore.getState().testConnection();
    expect(useGlobalStore.getState().connectionStatus).toBe("connecting");
    pending.resolve(alice); await connection;
    expect(useGlobalStore.getState().userInfo).toEqual(alice);
  });
  it("late identity cannot replace a newer credential's identity", async () => {
    const pending = deferred<NativeIdentity>();
    const old = { get: async (path: string) => path.endsWith("status") ? status : pending.promise } as FetchClient;
    useGlobalStore.setState({ client: old });
    const first = useGlobalStore.getState().testConnection();
    useGlobalStore.setState({ client: { get: async (path: string) => path.endsWith("status") ? status : bob } as FetchClient,
      config: { apiKey: "bob", baseUrl: "http://127.0.0.1:1" } });
    await useGlobalStore.getState().testConnection();
    pending.resolve(alice); await first;
    expect(useGlobalStore.getState().userInfo).toEqual(bob);
  });
  it("disconnect discards an in-flight native identity", async () => {
    const pending = deferred<NativeIdentity>();
    useGlobalStore.setState({ client: { get: async (path: string) => path.endsWith("status") ? status : pending.promise } as FetchClient });
    const first = useGlobalStore.getState().testConnection();
    useGlobalStore.getState().setConnectionStatus("disconnected");
    pending.resolve(alice); await first;
    expect(useGlobalStore.getState().userInfo).toBeNull();
    expect(useGlobalStore.getState().connectionStatus).toBe("disconnected");
  });
});
