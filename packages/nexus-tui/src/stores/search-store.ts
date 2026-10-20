import { SearchClient, type FetchClient, type SearchConnection, type SearchHit } from "#nexus-api-client";
import { createStore } from "./create-store.js";
export type SearchResult = SearchHit;
export type SearchMode = "keyword" | "semantic" | "hybrid";
const modes: SearchMode[] = ["keyword", "semantic", "hybrid"];
let controller: AbortController | null = null;
let requestId = 0;
interface SearchState {
  searchQuery: string; searchResults: readonly SearchHit[]; searchTotal: number; selectedResultIndex: number;
  searchLoading: boolean; error: string | null; searchMode: SearchMode;
  search: (query: string, client: FetchClient, connection?: SearchConnection) => Promise<void>;
  setSearchMode: (mode: SearchMode) => void; cycleSearchMode: () => void;
  setSelectedResultIndex: (index: number) => void; reset: () => void; cancel: () => void;
}
const initial = { searchQuery: "", searchResults: [] as readonly SearchHit[], searchTotal: 0, selectedResultIndex: 0,
  searchLoading: false, error: null, searchMode: "keyword" as SearchMode };
export const useSearchStore = createStore<SearchState>((set, get) => ({
  ...initial,
  search: async (query, client, connection) => {
    controller?.abort();
    const active = new AbortController(); controller = active;
    const id = ++requestId;
    set({ searchQuery: query, searchLoading: true, error: null, searchResults: [], searchTotal: 0 });
    try {
      const response = await new SearchClient(client, connection, { signal: active.signal }).query(query, get().searchMode);
      if (id !== requestId || active.signal.aborted) return;
      if (response.error) throw new Error(response.error);
      if (!Array.isArray(response.results) || !response.results.every((hit) => hit && typeof hit.path === "string"
        && typeof hit.chunk_text === "string" && typeof hit.zone_id === "string" && Number.isFinite(hit.score)))
        throw new Error("Invalid native Search response");
      set({ searchResults: response.results, searchTotal: response.results.length, selectedResultIndex: 0, searchLoading: false });
    } catch (error) {
      if (id !== requestId || active.signal.aborted) return;
      set({ searchLoading: false, error: error instanceof Error ? error.message : String(error) });
    }
  },
  setSearchMode: (searchMode) => set({ searchMode }),
  cycleSearchMode: () => set({ searchMode: modes[(modes.indexOf(get().searchMode) + 1) % modes.length]! }),
  setSelectedResultIndex: (selectedResultIndex) => set({ selectedResultIndex }),
  cancel: () => { controller?.abort(); ++requestId; set({ searchLoading: false }); },
  reset: () => { controller?.abort(); ++requestId; set(initial); },
}));
