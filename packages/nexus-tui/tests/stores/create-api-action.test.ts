/**
 * Tests for the createApiAction helper (Decision 6A).
 *
 * Validates the shared try/catch/loading boilerplate that all store
 * actions delegate to.
 */

import { describe, it, expect, mock, beforeEach } from "bun:test";
import { createApiAction } from "../../src/stores/create-api-action.js";
import { useUiStore } from "../../src/stores/ui-store.js";
import type { FetchClient } from "@nexus-ai-fs/api-client";

// =============================================================================
// Test helpers
// =============================================================================

interface TestState {
  items: string[];
  itemsLoading: boolean;
  error: string | null;
}

function createTestStore() {
  let state: TestState = {
    items: [],
    itemsLoading: false,
    error: null,
  };

  const set = (partial: Partial<TestState>) => {
    state = { ...state, ...partial };
  };

  return { get: () => state, set };
}

function mockClient(response: unknown): FetchClient {
  return {
    get: mock(async () => response),
    post: mock(async () => response),
    delete: mock(async () => response),
    patch: mock(async () => response),
  } as unknown as FetchClient;
}

// =============================================================================
// Tests
// =============================================================================

describe("createApiAction", () => {
  beforeEach(() => {
    useUiStore.setState({ panelDataTimestamps: {}, panelVisitTimestamps: {} });
  });

  it("sets loading to true before the action", async () => {
    const store = createTestStore();
    const loadingStates: boolean[] = [];

    const originalSet = store.set;
    store.set = (partial: Partial<TestState>) => {
      originalSet(partial);
      if ("itemsLoading" in partial) {
        loadingStates.push(partial.itemsLoading!);
      }
    };

    const action = createApiAction<TestState>(store.set, {
      loadingKey: "itemsLoading",
      action: async () => ({ items: ["a", "b"] }),
    });

    const client = mockClient({});
    await action(client);

    // First call sets loading=true, second sets loading=false
    expect(loadingStates).toEqual([true, false]);
  });

  it("merges action result into state on success", async () => {
    const store = createTestStore();
    const action = createApiAction<TestState>(store.set, {
      loadingKey: "itemsLoading",
      action: async (client) => {
        const data = await client.get<string[]>("/api/v2/items");
        return { items: data };
      },
    });

    const client = mockClient(["item1", "item2"]);
    await action(client);

    expect(store.get().items).toEqual(["item1", "item2"]);
    expect(store.get().itemsLoading).toBe(false);
    expect(store.get().error).toBeNull();
  });

  it("sets error message on failure", async () => {
    const store = createTestStore();
    const action = createApiAction<TestState>(store.set, {
      loadingKey: "itemsLoading",
      action: async () => {
        throw new Error("Server error");
      },
    });

    const client = mockClient({});
    await action(client);

    expect(store.get().error).toBe("Server error");
    expect(store.get().itemsLoading).toBe(false);
  });

  it("uses errorMessage fallback for non-Error throws", async () => {
    const store = createTestStore();
    const action = createApiAction<TestState>(store.set, {
      loadingKey: "itemsLoading",
      action: async () => {
        throw "string error";
      },
      errorMessage: "Custom error message",
    });

    const client = mockClient({});
    await action(client);

    expect(store.get().error).toBe("Custom error message");
  });

  it("uses default fallback when no errorMessage provided", async () => {
    const store = createTestStore();
    const action = createApiAction<TestState>(store.set, {
      loadingKey: "itemsLoading",
      action: async () => {
        throw 42;
      },
    });

    const client = mockClient({});
    await action(client);

    expect(store.get().error).toBe("Operation failed");
  });

  it("clears error before starting action", async () => {
    const store = createTestStore();
    store.set({ error: "previous error" });

    const action = createApiAction<TestState>(store.set, {
      loadingKey: "itemsLoading",
      action: async () => ({ items: ["ok"] }),
    });

    const client = mockClient({});
    await action(client);

    expect(store.get().error).toBeNull();
  });

  it("passes extra arguments to the action function", async () => {
    const store = createTestStore();
    const receivedArgs: unknown[] = [];

    const action = createApiAction<TestState>(store.set, {
      loadingKey: "itemsLoading",
      action: async (_client, ...args) => {
        receivedArgs.push(...args);
        return { items: [] };
      },
    });

    const client = mockClient({});
    await action(client, "arg1", 42);

    expect(receivedArgs).toEqual(["arg1", 42]);
  });

  it("marks data updated in ui-store when source is provided", async () => {
    const store = createTestStore();
    const action = createApiAction<TestState>(store.set, {
      loadingKey: "itemsLoading",
      action: async () => ({ items: ["a"] }),
      source: "files",
    });

    const client = mockClient({});
    await action(client);

    const ts = useUiStore.getState().panelDataTimestamps["files"];
    expect(ts).toBeDefined();
    expect(ts!).toBeGreaterThan(0);
  });

  it("does not mark data updated when source is omitted", async () => {
    const store = createTestStore();
    const action = createApiAction<TestState>(store.set, {
      loadingKey: "itemsLoading",
      action: async () => ({ items: ["a"] }),
    });

    const client = mockClient({});
    await action(client);

    expect(useUiStore.getState().panelDataTimestamps["files"]).toBeUndefined();
  });

  it("does not mark data updated on failure", async () => {
    const store = createTestStore();
    const action = createApiAction<TestState>(store.set, {
      loadingKey: "itemsLoading",
      action: async () => { throw new Error("fail"); },
      source: "versions",
    });

    const client = mockClient({});
    await action(client);

    expect(useUiStore.getState().panelDataTimestamps["versions"]).toBeUndefined();
  });
});
