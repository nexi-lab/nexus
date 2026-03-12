import { describe, it, expect, beforeEach, mock } from "bun:test";
import { useFilesStore } from "../../src/stores/files-store.js";
import type { FetchClient } from "@nexus/api-client";

function mockClient(responses: Record<string, unknown>): FetchClient {
  return {
    get: mock(async (path: string) => {
      for (const [pattern, response] of Object.entries(responses)) {
        if (path.includes(pattern)) return response;
      }
      throw new Error(`Unmocked path: ${path}`);
    }),
  } as unknown as FetchClient;
}

describe("FilesStore", () => {
  beforeEach(() => {
    useFilesStore.setState({
      fileCache: new Map(),
      currentPath: "/",
      selectedIndex: 0,
      treeNodes: new Map(),
      focusPane: "tree",
      previewPath: null,
      previewContent: null,
      previewLoading: false,
      error: null,
    });
  });

  describe("setCurrentPath", () => {
    it("changes path and resets selected index", () => {
      useFilesStore.getState().setSelectedIndex(5);
      useFilesStore.getState().setCurrentPath("/docs");
      const state = useFilesStore.getState();
      expect(state.currentPath).toBe("/docs");
      expect(state.selectedIndex).toBe(0);
    });
  });

  describe("setFocusPane", () => {
    it("switches between tree and preview", () => {
      useFilesStore.getState().setFocusPane("preview");
      expect(useFilesStore.getState().focusPane).toBe("preview");
      useFilesStore.getState().setFocusPane("tree");
      expect(useFilesStore.getState().focusPane).toBe("tree");
    });
  });

  describe("fetchFiles", () => {
    it("fetches and sorts files (directories first)", async () => {
      const client = mockClient({
        "/api/v2/files/list": {
          items: [
            { name: "z.txt", path: "/z.txt", isDirectory: false, size: 100, modifiedAt: null, etag: null, mimeType: null },
            { name: "a_dir", path: "/a_dir", isDirectory: true, size: 0, modifiedAt: null, etag: null, mimeType: null },
            { name: "b.txt", path: "/b.txt", isDirectory: false, size: 200, modifiedAt: null, etag: null, mimeType: null },
          ],
        },
      });

      await useFilesStore.getState().fetchFiles("/", client);
      const cached = useFilesStore.getState().fileCache.get("/");
      expect(cached).toBeDefined();
      expect(cached!.data[0]!.name).toBe("a_dir");
      expect(cached!.data[0]!.isDirectory).toBe(true);
      expect(cached!.data[1]!.name).toBe("b.txt");
      expect(cached!.data[2]!.name).toBe("z.txt");
    });

    it("uses SWR cache (doesn't re-fetch within TTL)", async () => {
      const client = mockClient({
        "/api/v2/files/list": { items: [] },
      });

      await useFilesStore.getState().fetchFiles("/", client);
      await useFilesStore.getState().fetchFiles("/", client);
      // Should only be called once due to caching
      expect((client.get as ReturnType<typeof mock>).mock.calls.length).toBe(1);
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Network error"); }),
      } as unknown as FetchClient;

      await useFilesStore.getState().fetchFiles("/", client);
      expect(useFilesStore.getState().error).toBe("Network error");
    });
  });

  describe("invalidate", () => {
    it("removes path from cache", async () => {
      const client = mockClient({
        "/api/v2/files/list": { items: [] },
      });
      await useFilesStore.getState().fetchFiles("/", client);
      expect(useFilesStore.getState().fileCache.has("/")).toBe(true);

      useFilesStore.getState().invalidate("/");
      expect(useFilesStore.getState().fileCache.has("/")).toBe(false);
    });
  });

  describe("tree operations", () => {
    it("expandNode creates tree nodes for children", async () => {
      const client = mockClient({
        "/api/v2/files/list": {
          items: [
            { name: "child.txt", path: "/child.txt", isDirectory: false, size: 100, modifiedAt: null, etag: null, mimeType: null },
            { name: "subdir", path: "/subdir", isDirectory: true, size: 0, modifiedAt: null, etag: null, mimeType: null },
          ],
        },
      });

      await useFilesStore.getState().expandNode("/", client);

      const nodes = useFilesStore.getState().treeNodes;
      const root = nodes.get("/");
      expect(root).toBeDefined();
      expect(root!.expanded).toBe(true);
      expect(root!.loading).toBe(false);
      expect(root!.children).toContain("/subdir");
      expect(root!.children).toContain("/child.txt");

      // Children should exist as nodes
      const subdir = nodes.get("/subdir");
      expect(subdir).toBeDefined();
      expect(subdir!.isDirectory).toBe(true);
      expect(subdir!.depth).toBe(1);
    });

    it("collapseNode sets expanded to false", async () => {
      const client = mockClient({
        "/api/v2/files/list": { items: [] },
      });

      await useFilesStore.getState().expandNode("/", client);
      expect(useFilesStore.getState().treeNodes.get("/")!.expanded).toBe(true);

      useFilesStore.getState().collapseNode("/");
      expect(useFilesStore.getState().treeNodes.get("/")!.expanded).toBe(false);
    });
  });

  describe("fetchPreview", () => {
    it("sets preview content", async () => {
      const client = mockClient({
        "/api/v2/files/read": { content: "Hello World" },
      });

      await useFilesStore.getState().fetchPreview("/test.txt", client);
      const state = useFilesStore.getState();
      expect(state.previewPath).toBe("/test.txt");
      expect(state.previewContent).toBe("Hello World");
      expect(state.previewLoading).toBe(false);
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Not found"); }),
      } as unknown as FetchClient;

      await useFilesStore.getState().fetchPreview("/missing.txt", client);
      expect(useFilesStore.getState().previewContent).toBeNull();
      expect(useFilesStore.getState().error).toBe("Not found");
    });
  });
});
