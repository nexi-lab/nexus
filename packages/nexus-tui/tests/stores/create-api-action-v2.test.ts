/**
 * Tests for enhanced createApiAction — onSuccess callback, error categorization,
 * and integration with the centralized error store.
 *
 * Written test-first (Decision 10A). Supplements existing create-api-action.test.ts.
 */

import { describe, it, expect, beforeEach, mock } from "bun:test";
import { createApiAction } from "../../src/stores/create-api-action.js";
import { useErrorStore } from "../../src/stores/error-store.js";
import type { FetchClient } from "@nexus/api-client";

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
// New feature tests
// =============================================================================

describe("createApiAction (v2 enhancements)", () => {
  beforeEach(() => {
    useErrorStore.setState({ errors: [] });
  });

  describe("onSuccess callback", () => {
    it("calls onSuccess after successful action", async () => {
      const store = createTestStore();
      const onSuccess = mock(() => {});

      const action = createApiAction<TestState>(store.set, {
        loadingKey: "itemsLoading",
        action: async () => ({ items: ["ok"] }),
        onSuccess,
      });

      await action(mockClient({}));
      expect(onSuccess).toHaveBeenCalledTimes(1);
    });

    it("does not call onSuccess on failure", async () => {
      const store = createTestStore();
      const onSuccess = mock(() => {});

      const action = createApiAction<TestState>(store.set, {
        loadingKey: "itemsLoading",
        action: async () => { throw new Error("fail"); },
        onSuccess,
      });

      await action(mockClient({}));
      expect(onSuccess).toHaveBeenCalledTimes(0);
    });
  });

  describe("error store integration", () => {
    it("pushes structured error to error store on failure", async () => {
      const store = createTestStore();

      const action = createApiAction<TestState>(store.set, {
        loadingKey: "itemsLoading",
        action: async () => { throw new Error("Server error"); },
        source: "payments",
      });

      await action(mockClient({}));

      const errors = useErrorStore.getState().errors;
      expect(errors).toHaveLength(1);
      expect(errors[0]!.message).toBe("Server error");
      expect(errors[0]!.source).toBe("payments");
      expect(errors[0]!.category).toBe("server");
    });

    it("categorizes network errors from error message patterns", async () => {
      const store = createTestStore();

      const action = createApiAction<TestState>(store.set, {
        loadingKey: "itemsLoading",
        action: async () => { throw new Error("fetch failed: ECONNREFUSED"); },
      });

      await action(mockClient({}));

      const errors = useErrorStore.getState().errors;
      expect(errors[0]!.category).toBe("network");
    });

    it("stores retry action when retryable is true", async () => {
      const store = createTestStore();
      const client = mockClient({});

      const action = createApiAction<TestState>(store.set, {
        loadingKey: "itemsLoading",
        action: async () => { throw new Error("timeout"); },
        retryable: true,
      });

      await action(client);

      const errors = useErrorStore.getState().errors;
      expect(errors[0]!.retryAction).toBeDefined();
    });

    it("does not push to error store when pushToErrorStore is false", async () => {
      const store = createTestStore();

      const action = createApiAction<TestState>(store.set, {
        loadingKey: "itemsLoading",
        action: async () => { throw new Error("expected error"); },
        pushToErrorStore: false,
      });

      await action(mockClient({}));

      expect(useErrorStore.getState().errors).toHaveLength(0);
      // But still sets local error
      expect(store.get().error).toBe("expected error");
    });
  });

  describe("backward compatibility", () => {
    it("still works without new options", async () => {
      const store = createTestStore();

      const action = createApiAction<TestState>(store.set, {
        loadingKey: "itemsLoading",
        action: async () => ({ items: ["a"] }),
      });

      await action(mockClient({}));
      expect(store.get().items).toEqual(["a"]);
      expect(store.get().itemsLoading).toBe(false);
    });

    it("still sets local error string", async () => {
      const store = createTestStore();

      const action = createApiAction<TestState>(store.set, {
        loadingKey: "itemsLoading",
        action: async () => { throw new Error("fail"); },
      });

      await action(mockClient({}));
      expect(store.get().error).toBe("fail");
    });
  });
});
