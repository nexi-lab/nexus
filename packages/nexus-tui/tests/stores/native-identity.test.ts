import { beforeEach, describe, expect, it } from "bun:test";
import type { FetchClient, NativeIdentity, NexusClientOptions } from "@nexus-ai-fs/api-client";
import { useGlobalStore } from "../../src/stores/global-store.js";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

const alice: NativeIdentity = {
  subject_type: "user", subject_id: "alice", user_id: "alice", agent_id: null,
  zone_id: "sharedzone", is_admin: false, zone_perms: [["sharedzone", "r"]],
};
const bob: NativeIdentity = { ...alice, subject_id: "bob", user_id: "bob" };
const status = { status: "ok", version: "0.10.1" };
const settle = () => new Promise<void>((resolve) => setImmediate(resolve));

describe("native identity lifecycle", () => {
  beforeEach(() => {
    useGlobalStore.setState({
      client: null, connectionStatus: "disconnected", connectionError: null,
      userInfo: null, serverVersion: null, featuresLastFetched: Date.now(),
      config: { apiKey: "fixture-key", baseUrl: "http://127.0.0.1:1", fetch: (async () => Response.json(null)) as typeof fetch },
    });
  });

  it("a slow feature endpoint does not delay a native connection", async () => {
    const features = deferred<unknown>();
    const client = { get: async (path: string) => path === "/v2/status" ? status : features.promise } as FetchClient;
    useGlobalStore.setState({ client, featuresLastFetched: 0 });
    await useGlobalStore.getState().testConnection();
    expect(useGlobalStore.getState().connectionStatus).toBe("connected");
    expect(useGlobalStore.getState().serverVersion).toBe("0.10.1");
    features.resolve(null);
  });

  it("an old identity response cannot overwrite a new credential on the same worker client", async () => {
    const previous = deferred<NativeIdentity>();
    const started = deferred<void>();
    const client = { get: async () => status } as FetchClient;
    const config: NexusClientOptions = {
      apiKey: "fixture-alice", baseUrl: "http://127.0.0.1:1", transformKeys: false,
      fetch: (async (_input: unknown, init: RequestInit) => {
        const key = new Headers(init.headers).get("authorization");
        if (key === "Bearer fixture-alice") {
          started.resolve();
          return Response.json(await previous.promise);
        }
        return Response.json(bob);
      }) as typeof fetch,
    };
    useGlobalStore.setState({ client, config });
    await useGlobalStore.getState().testConnection();
    await started.promise;
    useGlobalStore.setState({ config: { ...useGlobalStore.getState().config, apiKey: "fixture-bob" } });
    await useGlobalStore.getState().testConnection();
    await settle();
    expect(useGlobalStore.getState().userInfo).toEqual(bob);
    previous.resolve(alice);
    await settle();
    expect(useGlobalStore.getState().userInfo).toEqual(bob);
  });

  it("a late status failure cannot replace a newer successful connection", async () => {
    const previous = deferred<unknown>();
    let requests = 0;
    const client = { get: async () => ++requests === 1 ? previous.promise : status } as FetchClient;
    useGlobalStore.setState({ client });
    const first = useGlobalStore.getState().testConnection();
    await useGlobalStore.getState().testConnection();
    previous.resolve(null);
    await first;
    expect(useGlobalStore.getState().connectionStatus).toBe("connected");
    expect(useGlobalStore.getState().connectionError).toBeNull();
  });

  it("disconnecting discards an in-flight identity response", async () => {
    const identity = deferred<NativeIdentity>();
    const started = deferred<void>();
    const client = { get: async () => status } as FetchClient;
    const config: NexusClientOptions = {
      apiKey: "fixture-alice", baseUrl: "http://127.0.0.1:1",
      fetch: (async () => {
        started.resolve();
        return Response.json(await identity.promise);
      }) as typeof fetch,
    };
    useGlobalStore.setState({ client, config });
    await useGlobalStore.getState().testConnection();
    await started.promise;
    useGlobalStore.getState().setConnectionStatus("disconnected");
    identity.resolve(alice);
    await settle();
    expect(useGlobalStore.getState().userInfo).toBeNull();
    expect(useGlobalStore.getState().connectionStatus).toBe("disconnected");
  });
});
