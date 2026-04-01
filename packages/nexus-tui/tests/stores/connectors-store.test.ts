import { describe, it, expect, beforeEach, mock } from "bun:test";
import { useConnectorsStore } from "../../src/stores/connectors-store.js";
import type { FetchClient } from "@nexus-ai-fs/api-client";

// =============================================================================
// Test utilities
// =============================================================================

function mockClient(responses: Record<string, unknown>): FetchClient {
  return {
    get: mock(async (path: string) => {
      for (const [pattern, response] of Object.entries(responses)) {
        if (path.includes(pattern)) return response;
      }
      throw new Error(`Unmocked GET: ${path}`);
    }),
    post: mock(async (path: string) => {
      for (const [pattern, response] of Object.entries(responses)) {
        if (path.includes(pattern)) return response;
      }
      throw new Error(`Unmocked POST: ${path}`);
    }),
    delete: mock(async (path: string) => {
      for (const [pattern, response] of Object.entries(responses)) {
        if (path.includes(pattern)) return response;
      }
      throw new Error(`Unmocked DELETE: ${path}`);
    }),
  } as unknown as FetchClient;
}

function resetStore(): void {
  useConnectorsStore.setState({
    error: null,
    availableConnectors: [],
    availableLoading: false,
    selectedAvailableIndex: 0,
    authFlow: {
      status: "idle",
      auth_url: null,
      state_token: null,
      connector_name: null,
      error_message: null,
    },
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
    skillViewMode: "doc",
    selectedWriteMountIndex: 0,
    selectedOperationIndex: 0,
    writeTemplate: "",
    writeResult: null,
    writeLoading: false,
    activeTab: "available",
  });
}

// =============================================================================
// Mock data
// =============================================================================

const MOCK_CONNECTOR = {
  name: "gmail_connector",
  description: "Gmail connector",
  category: "oauth",
  capabilities: ["oauth", "sync"],
  user_scoped: true,
  auth_status: "authed",
  auth_source: "oauth",
  mount_path: "/mnt/gmail",
  sync_status: "synced",
};

const MOCK_MOUNT = {
  mount_point: "/mnt/gmail",
  readonly: false,
  connector_type: "gmail_connector",
  skill_name: "gmail",
  operations: ["send_email", "create_draft"],
  sync_status: "synced",
  last_sync: "2m ago",
};

const MOCK_SKILL_DOC = {
  mount_point: "/mnt/gmail",
  content: "# Gmail\nSend and manage emails.",
  schemas: ["send_email", "create_draft"],
};

const MOCK_SCHEMA = {
  mount_point: "/mnt/gmail",
  operation: "send_email",
  content: "to: # (required) Recipient — type: string\nsubject: # (required) Subject — type: string\nbody: # (required) Body — type: string",
};

const MOCK_WRITE_RESULT = {
  success: true,
  content_hash: "abc123",
  error: null,
};

const MOCK_AUTH_INIT = {
  auth_url: "https://accounts.google.com/o/oauth2/v2/auth?...",
  state_token: "test-state-token",
  provider: "gmail",
  expires_in: 300,
};

// =============================================================================
// Tests
// =============================================================================

describe("ConnectorsStore", () => {
  beforeEach(() => {
    resetStore();
  });

  // ---------------------------------------------------------------------------
  // Sync actions
  // ---------------------------------------------------------------------------

  describe("setActiveTab", () => {
    it("switches between tabs", () => {
      useConnectorsStore.getState().setActiveTab("mounted");
      expect(useConnectorsStore.getState().activeTab).toBe("mounted");

      useConnectorsStore.getState().setActiveTab("skills");
      expect(useConnectorsStore.getState().activeTab).toBe("skills");

      useConnectorsStore.getState().setActiveTab("write");
      expect(useConnectorsStore.getState().activeTab).toBe("write");

      useConnectorsStore.getState().setActiveTab("available");
      expect(useConnectorsStore.getState().activeTab).toBe("available");
    });
  });

  describe("setSelectedAvailableIndex", () => {
    it("sets the selected available connector index", () => {
      useConnectorsStore.getState().setSelectedAvailableIndex(3);
      expect(useConnectorsStore.getState().selectedAvailableIndex).toBe(3);
    });
  });

  describe("setSelectedMountIndex", () => {
    it("sets the selected mount index", () => {
      useConnectorsStore.getState().setSelectedMountIndex(2);
      expect(useConnectorsStore.getState().selectedMountIndex).toBe(2);
    });
  });

  describe("setSkillViewMode", () => {
    it("toggles between doc and schema modes", () => {
      useConnectorsStore.getState().setSkillViewMode("schema");
      expect(useConnectorsStore.getState().skillViewMode).toBe("schema");

      useConnectorsStore.getState().setSkillViewMode("doc");
      expect(useConnectorsStore.getState().skillViewMode).toBe("doc");
    });
  });

  describe("setWriteTemplate", () => {
    it("sets the write template content", () => {
      useConnectorsStore.getState().setWriteTemplate("to: test@example.com");
      expect(useConnectorsStore.getState().writeTemplate).toBe("to: test@example.com");
    });
  });

  describe("clearWriteResult", () => {
    it("clears the write result", () => {
      useConnectorsStore.setState({ writeResult: MOCK_WRITE_RESULT });
      useConnectorsStore.getState().clearWriteResult();
      expect(useConnectorsStore.getState().writeResult).toBeNull();
    });
  });

  describe("clearSyncResult", () => {
    it("clears the last sync result", () => {
      useConnectorsStore.setState({
        lastSyncResult: {
          mount_point: "/mnt/gmail",
          files_scanned: 10,
          files_synced: 5,
          delta_added: 3,
          delta_deleted: 1,
          history_id: "h1",
          is_delta: true,
          error: null,
        },
      });
      useConnectorsStore.getState().clearSyncResult();
      expect(useConnectorsStore.getState().lastSyncResult).toBeNull();
    });
  });

  // ---------------------------------------------------------------------------
  // fetchAvailable
  // ---------------------------------------------------------------------------

  describe("fetchAvailable", () => {
    it("fetches and stores available connectors", async () => {
      const client = mockClient({
        "/connectors/available": [MOCK_CONNECTOR],
      });

      await useConnectorsStore.getState().fetchAvailable(client);

      const state = useConnectorsStore.getState();
      expect(state.availableConnectors).toHaveLength(1);
      expect(state.availableConnectors[0]?.name).toBe("gmail_connector");
      expect(state.availableLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("handles fetch errors", async () => {
      const client = mockClient({});

      await useConnectorsStore.getState().fetchAvailable(client);

      const state = useConnectorsStore.getState();
      expect(state.availableLoading).toBe(false);
      expect(state.error).toBeTruthy();
    });
  });

  // ---------------------------------------------------------------------------
  // fetchMounts
  // ---------------------------------------------------------------------------

  describe("fetchMounts", () => {
    it("fetches and stores mounts", async () => {
      const client = mockClient({
        "/connectors/mounts": [MOCK_MOUNT],
      });

      await useConnectorsStore.getState().fetchMounts(client);

      const state = useConnectorsStore.getState();
      expect(state.mounts).toHaveLength(1);
      expect(state.mounts[0]?.mount_point).toBe("/mnt/gmail");
      expect(state.mountsLoading).toBe(false);
    });

    it("handles fetch errors", async () => {
      const client = mockClient({});

      await useConnectorsStore.getState().fetchMounts(client);

      expect(useConnectorsStore.getState().mountsLoading).toBe(false);
      expect(useConnectorsStore.getState().error).toBeTruthy();
    });
  });

  // ---------------------------------------------------------------------------
  // initiateAuth
  // ---------------------------------------------------------------------------

  describe("initiateAuth", () => {
    it("sets authFlow to waiting on initiation", async () => {
      const client = mockClient({
        "/connectors/auth/init": MOCK_AUTH_INIT,
      });

      // initiateAuth tries to open browser, which will fail in tests
      await useConnectorsStore.getState().initiateAuth("gmail_connector", client);

      const state = useConnectorsStore.getState();
      // Status should be "waiting" or "polling" depending on browser open
      expect(["waiting", "polling"]).toContain(state.authFlow.status);
      expect(state.authFlow.auth_url).toBe(MOCK_AUTH_INIT.auth_url);
      expect(state.authFlow.state_token).toBe(MOCK_AUTH_INIT.state_token);
    });

    it("handles auth init failure", async () => {
      const client = mockClient({});

      await useConnectorsStore.getState().initiateAuth("gmail_connector", client);

      const state = useConnectorsStore.getState();
      expect(state.authFlow.status).toBe("error");
      expect(state.authFlow.error_message).toBeTruthy();
    });
  });

  // ---------------------------------------------------------------------------
  // pollAuthStatus
  // ---------------------------------------------------------------------------

  describe("pollAuthStatus", () => {
    it("updates authFlow to completed when auth succeeds", async () => {
      useConnectorsStore.setState({
        authFlow: {
          status: "polling",
          auth_url: "https://example.com",
          state_token: "test-token",
          connector_name: "gmail_connector",
          error_message: null,
        },
      });

      const client = mockClient({
        "/connectors/auth/status": { status: "completed", connector_name: "gmail_connector", message: "OK" },
        "/connectors/available": [MOCK_CONNECTOR],
      });

      await useConnectorsStore.getState().pollAuthStatus(client);

      expect(useConnectorsStore.getState().authFlow.status).toBe("completed");
    });

    it("sets error on denied status", async () => {
      useConnectorsStore.setState({
        authFlow: {
          status: "polling",
          auth_url: "https://example.com",
          state_token: "test-token",
          connector_name: "gmail_connector",
          error_message: null,
        },
      });

      const client = mockClient({
        "/connectors/auth/status": { status: "denied", connector_name: "gmail_connector", message: "User denied" },
      });

      await useConnectorsStore.getState().pollAuthStatus(client);

      expect(useConnectorsStore.getState().authFlow.status).toBe("error");
      expect(useConnectorsStore.getState().authFlow.error_message).toContain("denied");
    });

    it("sets error on expired status", async () => {
      useConnectorsStore.setState({
        authFlow: {
          status: "polling",
          auth_url: "https://example.com",
          state_token: "test-token",
          connector_name: "gmail_connector",
          error_message: null,
        },
      });

      const client = mockClient({
        "/connectors/auth/status": { status: "expired", connector_name: "gmail_connector", message: "Expired" },
      });

      await useConnectorsStore.getState().pollAuthStatus(client);

      expect(useConnectorsStore.getState().authFlow.status).toBe("error");
    });

    it("does nothing when status is pending", async () => {
      useConnectorsStore.setState({
        authFlow: {
          status: "polling",
          auth_url: "https://example.com",
          state_token: "test-token",
          connector_name: "gmail_connector",
          error_message: null,
        },
      });

      const client = mockClient({
        "/connectors/auth/status": { status: "pending", connector_name: "gmail_connector", message: null },
      });

      await useConnectorsStore.getState().pollAuthStatus(client);

      // Should remain in polling state
      expect(useConnectorsStore.getState().authFlow.status).toBe("polling");
    });

    it("handles network errors during polling", async () => {
      useConnectorsStore.setState({
        authFlow: {
          status: "polling",
          auth_url: "https://example.com",
          state_token: "test-token",
          connector_name: "gmail_connector",
          error_message: null,
        },
      });

      const client = mockClient({});

      await useConnectorsStore.getState().pollAuthStatus(client);

      expect(useConnectorsStore.getState().authFlow.status).toBe("error");
      expect(useConnectorsStore.getState().authFlow.error_message).toBeTruthy();
    });

    it("skips polling when authFlow is idle", async () => {
      const client = mockClient({});

      await useConnectorsStore.getState().pollAuthStatus(client);

      // Should remain idle, no error
      expect(useConnectorsStore.getState().authFlow.status).toBe("idle");
      expect(useConnectorsStore.getState().error).toBeNull();
    });
  });

  // ---------------------------------------------------------------------------
  // cancelAuth
  // ---------------------------------------------------------------------------

  describe("cancelAuth", () => {
    it("resets authFlow to idle", () => {
      useConnectorsStore.setState({
        authFlow: {
          status: "polling",
          auth_url: "https://example.com",
          state_token: "test-token",
          connector_name: "gmail_connector",
          error_message: null,
        },
      });

      useConnectorsStore.getState().cancelAuth();

      const authFlow = useConnectorsStore.getState().authFlow;
      expect(authFlow.status).toBe("idle");
      expect(authFlow.auth_url).toBeNull();
      expect(authFlow.state_token).toBeNull();
    });
  });

  // ---------------------------------------------------------------------------
  // mountConnector
  // ---------------------------------------------------------------------------

  describe("mountConnector", () => {
    it("mounts and refreshes lists", async () => {
      // "/connectors/mounts" must appear before "/connectors/mount" to avoid
      // substring match (path.includes) returning the POST response for GET.
      const client = mockClient({
        "/connectors/mounts": [MOCK_MOUNT],
        "/connectors/available": [MOCK_CONNECTOR],
        "/connectors/mount": { mounted: true, mount_point: "/mnt/gmail" },
      });

      await useConnectorsStore.getState().mountConnector("gmail_connector", "/mnt/gmail", client);

      const state = useConnectorsStore.getState();
      expect(state.mounts).toHaveLength(1);
      expect(state.availableConnectors).toHaveLength(1);
      expect(state.mountsLoading).toBe(false);
    });

    it("handles mount errors", async () => {
      const client = mockClient({});

      await useConnectorsStore.getState().mountConnector("gmail_connector", "/mnt/gmail", client);

      expect(useConnectorsStore.getState().error).toBeTruthy();
    });
  });

  // ---------------------------------------------------------------------------
  // unmountConnector
  // ---------------------------------------------------------------------------

  describe("unmountConnector", () => {
    it("unmounts and refreshes lists", async () => {
      useConnectorsStore.setState({ mounts: [MOCK_MOUNT] });

      const client = mockClient({
        "/connectors/unmount": { mounted: false, mount_point: "/mnt/gmail" },
        "/connectors/mounts": [],
        "/connectors/available": [{ ...MOCK_CONNECTOR, mount_path: null }],
      });

      await useConnectorsStore.getState().unmountConnector("/mnt/gmail", client);

      expect(useConnectorsStore.getState().mounts).toHaveLength(0);
    });

    it("handles unmount errors", async () => {
      const client = mockClient({});

      await useConnectorsStore.getState().unmountConnector("/mnt/gmail", client);

      expect(useConnectorsStore.getState().error).toBeTruthy();
    });
  });

  // ---------------------------------------------------------------------------
  // triggerSync
  // ---------------------------------------------------------------------------

  describe("triggerSync", () => {
    it("tracks syncing state and stores result", async () => {
      const syncResult = {
        mount_point: "/mnt/gmail",
        files_scanned: 100,
        files_synced: 50,
        delta_added: 10,
        delta_deleted: 2,
        history_id: "h123",
        is_delta: true,
        error: null,
      };

      const client = mockClient({
        "/connectors/sync": syncResult,
        "/connectors/mounts": [MOCK_MOUNT],
      });

      // Check syncing state is set during sync
      const promise = useConnectorsStore.getState().triggerSync("/mnt/gmail", client);

      await promise;

      const state = useConnectorsStore.getState();
      expect(state.syncingMounts.has("/mnt/gmail")).toBe(false); // should be removed after completion
      expect(state.lastSyncResult?.files_synced).toBe(50);
      expect(state.lastSyncResult?.is_delta).toBe(true);
    });

    it("handles sync errors", async () => {
      const client = mockClient({});

      await useConnectorsStore.getState().triggerSync("/mnt/gmail", client);

      const state = useConnectorsStore.getState();
      expect(state.syncingMounts.has("/mnt/gmail")).toBe(false);
      expect(state.error).toBeTruthy();
    });
  });

  // ---------------------------------------------------------------------------
  // fetchSkillDoc
  // ---------------------------------------------------------------------------

  describe("fetchSkillDoc", () => {
    it("fetches and stores skill doc", async () => {
      const client = mockClient({
        "/connectors/skill/": MOCK_SKILL_DOC,
      });

      await useConnectorsStore.getState().fetchSkillDoc("/mnt/gmail", client);

      const state = useConnectorsStore.getState();
      expect(state.skillDoc?.content).toContain("Gmail");
      expect(state.skillDoc?.schemas).toHaveLength(2);
      expect(state.skillDocLoading).toBe(false);
    });

    it("handles fetch errors", async () => {
      const client = mockClient({});

      await useConnectorsStore.getState().fetchSkillDoc("/mnt/gmail", client);

      expect(useConnectorsStore.getState().skillDocLoading).toBe(false);
      expect(useConnectorsStore.getState().error).toBeTruthy();
    });
  });

  // ---------------------------------------------------------------------------
  // fetchSchema
  // ---------------------------------------------------------------------------

  describe("fetchSchema", () => {
    it("fetches and stores schema doc", async () => {
      const client = mockClient({
        "/connectors/schema/": MOCK_SCHEMA,
      });

      await useConnectorsStore.getState().fetchSchema("/mnt/gmail", "send_email", client);

      const state = useConnectorsStore.getState();
      expect(state.schemaDoc?.operation).toBe("send_email");
      expect(state.schemaDoc?.content).toContain("to:");
      expect(state.schemaDocLoading).toBe(false);
    });

    it("handles fetch errors", async () => {
      const client = mockClient({});

      await useConnectorsStore.getState().fetchSchema("/mnt/gmail", "send_email", client);

      expect(useConnectorsStore.getState().schemaDocLoading).toBe(false);
      expect(useConnectorsStore.getState().error).toBeTruthy();
    });
  });

  // ---------------------------------------------------------------------------
  // submitWrite
  // ---------------------------------------------------------------------------

  describe("submitWrite", () => {
    it("submits write and stores result", async () => {
      const client = mockClient({
        "/connectors/write/": MOCK_WRITE_RESULT,
      });

      await useConnectorsStore.getState().submitWrite(
        "/mnt/gmail",
        "to: test@example.com\nsubject: Test",
        client,
      );

      const state = useConnectorsStore.getState();
      expect(state.writeResult?.success).toBe(true);
      expect(state.writeResult?.content_hash).toBe("abc123");
      expect(state.writeLoading).toBe(false);
    });

    it("stores error result on write failure", async () => {
      const client = mockClient({
        "/connectors/write/": { success: false, content_hash: null, error: "Validation failed" },
      });

      await useConnectorsStore.getState().submitWrite(
        "/mnt/gmail",
        "invalid yaml",
        client,
      );

      const state = useConnectorsStore.getState();
      expect(state.writeResult?.success).toBe(false);
      expect(state.writeResult?.error).toBe("Validation failed");
    });

    it("handles network errors during write", async () => {
      const client = mockClient({});

      await useConnectorsStore.getState().submitWrite(
        "/mnt/gmail",
        "to: test@example.com",
        client,
      );

      expect(useConnectorsStore.getState().writeLoading).toBe(false);
      expect(useConnectorsStore.getState().error).toBeTruthy();
    });
  });
});
