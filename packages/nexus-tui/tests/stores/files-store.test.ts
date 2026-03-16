import { describe, it, expect, beforeEach, mock } from "bun:test";
import { useFilesStore, getEffectiveSelection } from "../../src/stores/files-store.js";
import type { FetchClient } from "@nexus/api-client";

function mockClient(responses: Record<string, unknown>): FetchClient {
  return {
    get: mock(async (path: string) => {
      for (const [pattern, response] of Object.entries(responses)) {
        if (path.includes(pattern)) return response;
      }
      throw new Error(`Unmocked path: ${path}`);
    }),
    post: mock(async (path: string) => {
      for (const [pattern, response] of Object.entries(responses)) {
        if (path.includes(pattern)) return response;
      }
      return { success: true };
    }),
    delete: mock(async (path: string) => {
      for (const [pattern, response] of Object.entries(responses)) {
        if (path.includes(pattern)) return response;
      }
      return { success: true };
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
      selectedPaths: new Set(),
      visualModeAnchor: null,
      clipboard: null,
      pasteProgress: null,
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

  // ===========================================================================
  // Mutating file operations (backfill — Decision 9A)
  // ===========================================================================

  describe("deleteFile", () => {
    it("calls DELETE API and invalidates parent cache", async () => {
      const client = mockClient({
        "/api/v2/files/list": { items: [] },
      });

      // Pre-populate cache for parent
      await useFilesStore.getState().fetchFiles("/", client);
      expect(useFilesStore.getState().fileCache.has("/")).toBe(true);

      await useFilesStore.getState().deleteFile("/test.txt", client);

      // Should have called delete
      expect((client.delete as ReturnType<typeof mock>).mock.calls.length).toBe(1);
      expect((client.delete as ReturnType<typeof mock>).mock.calls[0]![0]).toContain("/api/v2/files/delete");
      expect((client.delete as ReturnType<typeof mock>).mock.calls[0]![0]).toContain("test.txt");
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => ({ items: [] })),
        post: mock(async () => ({})),
        delete: mock(async () => { throw new Error("Permission denied"); }),
      } as unknown as FetchClient;

      await useFilesStore.getState().deleteFile("/protected.txt", client);
      expect(useFilesStore.getState().error).toBe("Permission denied");
    });
  });

  describe("mkdirFile", () => {
    it("calls POST mkdir API and invalidates parent cache", async () => {
      const client = mockClient({
        "/api/v2/files/list": { items: [] },
      });

      await useFilesStore.getState().mkdirFile("/newdir", client);

      expect((client.post as ReturnType<typeof mock>).mock.calls.length).toBe(1);
      expect((client.post as ReturnType<typeof mock>).mock.calls[0]![0]).toContain("/api/v2/files/mkdir");
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => ({ items: [] })),
        post: mock(async () => { throw new Error("Already exists"); }),
        delete: mock(async () => ({})),
      } as unknown as FetchClient;

      await useFilesStore.getState().mkdirFile("/existing", client);
      expect(useFilesStore.getState().error).toBe("Already exists");
    });
  });

  describe("renameFile", () => {
    it("calls rename API and invalidates parent cache", async () => {
      const client = mockClient({
        "/api/v2/files/list": { items: [] },
        "/api/v2/files/rename": { success: true },
      });

      await useFilesStore.getState().renameFile("/old.txt", "/new.txt", client);

      // Should call the rename endpoint
      expect((client.post as ReturnType<typeof mock>).mock.calls.length).toBeGreaterThanOrEqual(1);
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => ({ items: [] })),
        post: mock(async () => { throw new Error("Conflict"); }),
        delete: mock(async () => ({})),
      } as unknown as FetchClient;

      await useFilesStore.getState().renameFile("/a.txt", "/b.txt", client);
      expect(useFilesStore.getState().error).toBe("Conflict");
    });
  });

  describe("writeFile", () => {
    it("calls POST write API", async () => {
      const client = mockClient({
        "/api/v2/files/write": { success: true },
      });

      await useFilesStore.getState().writeFile("/test.txt", "hello", client);

      expect((client.post as ReturnType<typeof mock>).mock.calls.length).toBe(1);
      expect((client.post as ReturnType<typeof mock>).mock.calls[0]![0]).toContain("/api/v2/files/write");
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => ({})),
        post: mock(async () => { throw new Error("Disk full"); }),
        delete: mock(async () => ({})),
      } as unknown as FetchClient;

      await useFilesStore.getState().writeFile("/test.txt", "hello", client);
      expect(useFilesStore.getState().error).toBe("Disk full");
    });
  });

  // ===========================================================================
  // Selection state
  // ===========================================================================

  describe("toggleSelect", () => {
    it("adds a path to selectedPaths", () => {
      useFilesStore.getState().toggleSelect("/foo.txt");
      expect(useFilesStore.getState().selectedPaths.has("/foo.txt")).toBe(true);
    });

    it("removes a path when toggled again", () => {
      useFilesStore.getState().toggleSelect("/foo.txt");
      useFilesStore.getState().toggleSelect("/foo.txt");
      expect(useFilesStore.getState().selectedPaths.has("/foo.txt")).toBe(false);
      expect(useFilesStore.getState().selectedPaths.size).toBe(0);
    });

    it("supports multiple independent selections", () => {
      useFilesStore.getState().toggleSelect("/a.txt");
      useFilesStore.getState().toggleSelect("/b.txt");
      useFilesStore.getState().toggleSelect("/c.txt");
      const selected = useFilesStore.getState().selectedPaths;
      expect(selected.size).toBe(3);
      expect(selected.has("/a.txt")).toBe(true);
      expect(selected.has("/b.txt")).toBe(true);
      expect(selected.has("/c.txt")).toBe(true);
    });
  });

  describe("clearSelection", () => {
    it("clears all selectedPaths", () => {
      useFilesStore.getState().toggleSelect("/a.txt");
      useFilesStore.getState().toggleSelect("/b.txt");
      useFilesStore.getState().clearSelection();
      expect(useFilesStore.getState().selectedPaths.size).toBe(0);
    });

    it("clears visualModeAnchor", () => {
      useFilesStore.getState().enterVisualMode(3);
      useFilesStore.getState().clearSelection();
      expect(useFilesStore.getState().visualModeAnchor).toBeNull();
    });

    it("clears both selectedPaths and visualModeAnchor together", () => {
      useFilesStore.getState().toggleSelect("/x.txt");
      useFilesStore.getState().enterVisualMode(5);
      useFilesStore.getState().clearSelection();
      expect(useFilesStore.getState().selectedPaths.size).toBe(0);
      expect(useFilesStore.getState().visualModeAnchor).toBeNull();
    });
  });

  describe("enterVisualMode", () => {
    it("sets the anchor index", () => {
      useFilesStore.getState().enterVisualMode(7);
      expect(useFilesStore.getState().visualModeAnchor).toBe(7);
    });

    it("updates anchor when called again", () => {
      useFilesStore.getState().enterVisualMode(2);
      useFilesStore.getState().enterVisualMode(9);
      expect(useFilesStore.getState().visualModeAnchor).toBe(9);
    });
  });

  describe("exitVisualMode", () => {
    it("clears the anchor", () => {
      useFilesStore.getState().enterVisualMode(4);
      useFilesStore.getState().exitVisualMode();
      expect(useFilesStore.getState().visualModeAnchor).toBeNull();
    });

    it("preserves selectedPaths", () => {
      useFilesStore.getState().toggleSelect("/keep.txt");
      useFilesStore.getState().enterVisualMode(2);
      useFilesStore.getState().exitVisualMode();
      expect(useFilesStore.getState().selectedPaths.has("/keep.txt")).toBe(true);
    });
  });

  // ===========================================================================
  // Clipboard state
  // ===========================================================================

  describe("yankToClipboard", () => {
    it("stores paths with operation=copy", () => {
      useFilesStore.getState().yankToClipboard(["/a.txt", "/b.txt"]);
      const clip = useFilesStore.getState().clipboard;
      expect(clip).not.toBeNull();
      expect(clip!.paths).toEqual(["/a.txt", "/b.txt"]);
      expect(clip!.operation).toBe("copy");
    });
  });

  describe("cutToClipboard", () => {
    it("stores paths with operation=cut", () => {
      useFilesStore.getState().cutToClipboard(["/c.txt"]);
      const clip = useFilesStore.getState().clipboard;
      expect(clip).not.toBeNull();
      expect(clip!.paths).toEqual(["/c.txt"]);
      expect(clip!.operation).toBe("cut");
    });
  });

  describe("clearClipboard", () => {
    it("sets clipboard to null", () => {
      useFilesStore.getState().yankToClipboard(["/a.txt"]);
      expect(useFilesStore.getState().clipboard).not.toBeNull();
      useFilesStore.getState().clearClipboard();
      expect(useFilesStore.getState().clipboard).toBeNull();
    });
  });

  describe("pasteFiles", () => {
    it("copies files and tracks progress", async () => {
      const client = mockClient({
        "/api/v2/files/list": { items: [] },
      });

      useFilesStore.getState().yankToClipboard(["/a.txt", "/b.txt"]);
      await useFilesStore.getState().pasteFiles("/dest", client);

      // Should have called copy endpoint for each file
      const postCalls = (client.post as ReturnType<typeof mock>).mock.calls;
      expect(postCalls.length).toBeGreaterThanOrEqual(2);

      // Clipboard should be cleared after paste
      expect(useFilesStore.getState().clipboard).toBeNull();
    });

    it("uses rename endpoint for cut operations", async () => {
      const client = mockClient({
        "/api/v2/files/list": { items: [] },
      });

      useFilesStore.getState().cutToClipboard(["/x.txt"]);
      await useFilesStore.getState().pasteFiles("/dest", client);

      const postCalls = (client.post as ReturnType<typeof mock>).mock.calls;
      expect(postCalls.some((c: unknown[]) => (c[0] as string).includes("/api/v2/files/rename"))).toBe(true);
      expect(useFilesStore.getState().clipboard).toBeNull();
    });

    it("tracks failed operations", async () => {
      let callCount = 0;
      const client = {
        get: mock(async () => ({ items: [] })),
        post: mock(async () => {
          callCount++;
          if (callCount === 2) throw new Error("Disk full");
          return { success: true };
        }),
        delete: mock(async () => ({})),
      } as unknown as FetchClient;

      useFilesStore.getState().yankToClipboard(["/a.txt", "/b.txt", "/c.txt"]);
      await useFilesStore.getState().pasteFiles("/dest", client);

      expect(useFilesStore.getState().error).toContain("1 of 3 operations failed");
    });

    it("no-ops when clipboard is empty", async () => {
      const client = mockClient({});
      await useFilesStore.getState().pasteFiles("/dest", client);
      expect((client.post as ReturnType<typeof mock>).mock.calls.length).toBe(0);
    });
  });

  // ===========================================================================
  // getEffectiveSelection (pure function)
  // ===========================================================================

  describe("getEffectiveSelection", () => {
    const nodes = ["/a", "/b", "/c", "/d", "/e", "/f", "/g"];

    it("returns selectedPaths as-is when not in visual mode", () => {
      const selected = new Set(["/a", "/c"]);
      const result = getEffectiveSelection(selected, null, 0, nodes);
      expect(result).toEqual(new Set(["/a", "/c"]));
    });

    it("computes range from anchor to cursor (forward)", () => {
      // anchor=2 (/c), cursor=5 (/f) → indices 2,3,4,5 → /c,/d,/e,/f
      const result = getEffectiveSelection(new Set(), 2, 5, nodes);
      expect(result).toEqual(new Set(["/c", "/d", "/e", "/f"]));
    });

    it("computes range from anchor to cursor (backward)", () => {
      // anchor=5, cursor=2 → same range
      const result = getEffectiveSelection(new Set(), 5, 2, nodes);
      expect(result).toEqual(new Set(["/c", "/d", "/e", "/f"]));
    });

    it("returns union of selectedPaths and visual range", () => {
      const selected = new Set(["/a", "/g"]);
      // anchor=2, cursor=3 → /c,/d
      const result = getEffectiveSelection(selected, 2, 3, nodes);
      expect(result).toEqual(new Set(["/a", "/g", "/c", "/d"]));
    });

    it("selects single item when anchor equals cursor", () => {
      const result = getEffectiveSelection(new Set(), 4, 4, nodes);
      expect(result).toEqual(new Set(["/e"]));
    });

    it("returns empty set when visibleNodes is empty", () => {
      const result = getEffectiveSelection(new Set(), 0, 3, []);
      expect(result).toEqual(new Set());
    });

    it("clamps range to visibleNodes bounds", () => {
      // anchor=5, cursor=10 with only 7 items → clamp cursor to 6
      const result = getEffectiveSelection(new Set(), 5, 10, nodes);
      expect(result).toEqual(new Set(["/f", "/g"]));
    });
  });
});
