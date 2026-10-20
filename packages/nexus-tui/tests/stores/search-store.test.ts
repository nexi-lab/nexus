import { beforeEach, describe, expect, it } from "bun:test";
import { FetchClient } from "@nexus-ai-fs/api-client";
import { useSearchStore } from "../../src/stores/search-store.js";
const hit = (path: string) => ({ path, chunk_index: 0, chunk_text: "native text", zone_id: "shared", score: 1 });
describe("native Search result lifetime", () => {
  beforeEach(() => useSearchStore.getState().reset());
  it("sends the original bearer, native mode and selected corpus", async () => {
    const requests: Array<{ url: string; init: RequestInit }> = [];
    const client = new FetchClient({ apiKey: "original-native", baseUrl: "http://127.0.0.1:1", maxRetries: 0,
      fetch: (async (url, init) => { requests.push({ url: String(url), init: init! }); return Response.json({ results: [hit("/visible")] }); }) as typeof fetch });
    await useSearchStore.getState().search("日本語 & text", client, { url: "http://127.0.0.1:2", zoneId: "shared" });
    expect(requests[0]!.url).toBe("http://127.0.0.1:2/v2/search/query");
    expect(new Headers(requests[0]!.init.headers).get("authorization")).toBe("Bearer original-native");
    expect(JSON.parse(String(requests[0]!.init.body))).toMatchObject({ q: "日本語 & text", query_type: "keyword", zone_id: "shared" });
    expect(useSearchStore.getState().searchResults.map((result) => result.path)).toEqual(["/visible"]);
  });
  for (const body of [{ error: "index unavailable", results: [] }, { results: [{ path: "/incomplete" }] }]) {
    it(`invalid or failed responses clear prior results: ${JSON.stringify(body)}`, async () => {
      useSearchStore.setState({ searchResults: [hit("/old")] });
      const client = { post: async () => body } as FetchClient;
      await useSearchStore.getState().search("next", client);
      expect(useSearchStore.getState().searchResults).toEqual([]);
      expect(useSearchStore.getState().error).toBeTruthy();
    });
  }
  it("late responses cannot restore old query or disconnected data", async () => {
    let finish!: (value: unknown) => void;
    const slow = new Promise((resolve) => { finish = resolve; });
    let signal: AbortSignal | undefined;
    const first = useSearchStore.getState().search("slow", { post: async (_path: string, _body: unknown, options: { signal: AbortSignal }) => {
      signal = options.signal; return slow;
    } } as FetchClient);
    await useSearchStore.getState().search("fast", { post: async () => ({ results: [hit("/new")] }) } as FetchClient);
    expect(signal!.aborted).toBe(true);
    finish({ results: [hit("/old")] }); await first;
    expect(useSearchStore.getState().searchResults.map((result) => result.path)).toEqual(["/new"]);
    useSearchStore.getState().reset();
    expect(useSearchStore.getState().searchResults).toEqual([]);
  });
});
