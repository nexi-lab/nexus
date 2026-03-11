/**
 * Search & Knowledge panel: tabbed layout with search, knowledge graph,
 * and memories views.
 *
 * Press / to enter search input mode, type query, Enter to submit, Escape to cancel.
 */

import React, { useState, useCallback } from "react";
import { useSearchStore } from "../../stores/search-store.js";
import type { SearchTab, SearchMode } from "../../stores/search-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { SearchResults } from "./search-results.js";
import { KnowledgeView } from "./knowledge-view.js";
import { MemoryList } from "./memory-list.js";

const TAB_ORDER: readonly SearchTab[] = ["search", "knowledge", "memories"];
const TAB_LABELS: Readonly<Record<SearchTab, string>> = {
  search: "Search",
  knowledge: "Knowledge",
  memories: "Memories",
};

const MODE_LABELS: Readonly<Record<SearchMode, string>> = {
  keyword: "KW",
  semantic: "SEM",
  hybrid: "HYB",
};

export default function SearchPanel(): React.ReactNode {
  const client = useApi();
  const [inputMode, setInputMode] = useState(false);
  const [inputBuffer, setInputBuffer] = useState("");

  const searchQuery = useSearchStore((s) => s.searchQuery);
  const searchResults = useSearchStore((s) => s.searchResults);
  const searchTotal = useSearchStore((s) => s.searchTotal);
  const selectedResultIndex = useSearchStore((s) => s.selectedResultIndex);
  const searchLoading = useSearchStore((s) => s.searchLoading);
  const selectedEntity = useSearchStore((s) => s.selectedEntity);
  const neighbors = useSearchStore((s) => s.neighbors);
  const knowledgeSearchResult = useSearchStore((s) => s.knowledgeSearchResult);
  const knowledgeLoading = useSearchStore((s) => s.knowledgeLoading);
  const memories = useSearchStore((s) => s.memories);
  const selectedMemoryIndex = useSearchStore((s) => s.selectedMemoryIndex);
  const memoriesLoading = useSearchStore((s) => s.memoriesLoading);
  const activeTab = useSearchStore((s) => s.activeTab);
  const error = useSearchStore((s) => s.error);

  const searchMode = useSearchStore((s) => s.searchMode);
  const cycleSearchMode = useSearchStore((s) => s.cycleSearchMode);

  const search = useSearchStore((s) => s.search);
  const fetchEntity = useSearchStore((s) => s.fetchEntity);
  const fetchNeighbors = useSearchStore((s) => s.fetchNeighbors);
  const searchKnowledge = useSearchStore((s) => s.searchKnowledge);
  const fetchMemories = useSearchStore((s) => s.fetchMemories);
  const setActiveTab = useSearchStore((s) => s.setActiveTab);
  const setSelectedResultIndex = useSearchStore((s) => s.setSelectedResultIndex);
  const setSelectedMemoryIndex = useSearchStore((s) => s.setSelectedMemoryIndex);
  const setSearchQuery = useSearchStore((s) => s.setSearchQuery);

  const submitSearch = useCallback(
    (query: string) => {
      if (!client || !query.trim()) return;

      setSearchQuery(query.trim());
      if (activeTab === "search") {
        search(query.trim(), client);
      } else if (activeTab === "knowledge") {
        searchKnowledge(query.trim(), client);
      } else if (activeTab === "memories") {
        fetchMemories(query.trim(), client);
      }
    },
    [client, activeTab, search, searchKnowledge, fetchMemories, setSearchQuery],
  );

  // Refresh current view based on active tab
  const refreshCurrentView = useCallback((): void => {
    if (!client) return;

    if (activeTab === "search" && searchQuery) {
      search(searchQuery, client);
    } else if (activeTab === "knowledge" && searchQuery) {
      searchKnowledge(searchQuery, client);
    } else if (activeTab === "memories") {
      fetchMemories("", client);
    }
  }, [client, activeTab, searchQuery, search, searchKnowledge, fetchMemories]);

  // In input mode, capture printable characters via onUnhandled
  const handleUnhandledKey = useCallback(
    (keyName: string) => {
      if (!inputMode) return;
      // Single printable character (letter, digit, symbol, space)
      if (keyName.length === 1) {
        setInputBuffer((b) => b + keyName);
      } else if (keyName === "space") {
        setInputBuffer((b) => b + " ");
      }
    },
    [inputMode],
  );

  useKeyboard(
    inputMode
      ? {
          // Input mode: capture keystrokes for the search query
          return: () => {
            setInputMode(false);
            submitSearch(inputBuffer);
          },
          escape: () => {
            setInputMode(false);
            setInputBuffer("");
          },
          backspace: () => {
            setInputBuffer((b) => b.slice(0, -1));
          },
        }
      : {
          // Normal mode: navigation
          j: () => {
            if (activeTab === "search") {
              setSelectedResultIndex(
                Math.min(selectedResultIndex + 1, searchResults.length - 1),
              );
            } else if (activeTab === "memories") {
              setSelectedMemoryIndex(
                Math.min(selectedMemoryIndex + 1, memories.length - 1),
              );
            }
          },
          down: () => {
            if (activeTab === "search") {
              setSelectedResultIndex(
                Math.min(selectedResultIndex + 1, searchResults.length - 1),
              );
            } else if (activeTab === "memories") {
              setSelectedMemoryIndex(
                Math.min(selectedMemoryIndex + 1, memories.length - 1),
              );
            }
          },
          k: () => {
            if (activeTab === "search") {
              setSelectedResultIndex(Math.max(selectedResultIndex - 1, 0));
            } else if (activeTab === "memories") {
              setSelectedMemoryIndex(Math.max(selectedMemoryIndex - 1, 0));
            }
          },
          up: () => {
            if (activeTab === "search") {
              setSelectedResultIndex(Math.max(selectedResultIndex - 1, 0));
            } else if (activeTab === "memories") {
              setSelectedMemoryIndex(Math.max(selectedMemoryIndex - 1, 0));
            }
          },
          tab: () => {
            const currentIdx = TAB_ORDER.indexOf(activeTab);
            const nextIdx = (currentIdx + 1) % TAB_ORDER.length;
            const nextTab = TAB_ORDER[nextIdx];
            if (nextTab) {
              setActiveTab(nextTab);
            }
          },
          r: () => refreshCurrentView(),
          m: () => cycleSearchMode(),
          "/": () => {
            setInputMode(true);
            setInputBuffer(searchQuery);
          },
          return: () => {
            if (!client) return;

            if (activeTab === "search") {
              const result = searchResults[selectedResultIndex];
              if (result) {
                fetchEntity(result.path, client);
                fetchNeighbors(result.path, client);
                setActiveTab("knowledge");
              }
            } else if (activeTab === "knowledge") {
              if (selectedEntity) {
                const entityId = String(
                  (selectedEntity as Record<string, unknown>).entity_id ?? "",
                );
                if (entityId) {
                  fetchNeighbors(entityId, client);
                }
              }
            }
          },
        },
    handleUnhandledKey,
  );

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Search input bar */}
      <box height={1} width="100%">
        <text>
          {inputMode
            ? `Search: ${inputBuffer}█`
            : `Query: ${searchQuery || "(press / to search)"}  [${MODE_LABELS[searchMode]}]`}
        </text>
      </box>

      {/* Tab bar */}
      <box height={1} width="100%">
        <text>
          {TAB_ORDER.map((tab) => {
            const label = TAB_LABELS[tab];
            return tab === activeTab ? `[${label}]` : ` ${label} `;
          }).join(" ")}
        </text>
      </box>

      {/* Error display */}
      {error && (
        <box height={1} width="100%">
          <text>{`Error: ${error}`}</text>
        </box>
      )}

      {/* Tab content */}
      <box flexGrow={1} borderStyle="single">
        {activeTab === "search" && (
          <SearchResults
            results={searchResults}
            total={searchTotal}
            selectedIndex={selectedResultIndex}
            loading={searchLoading}
          />
        )}
        {activeTab === "knowledge" && (
          <KnowledgeView
            entity={selectedEntity}
            neighbors={neighbors}
            knowledgeSearchResult={knowledgeSearchResult}
            loading={knowledgeLoading}
          />
        )}
        {activeTab === "memories" && (
          <MemoryList
            memories={memories}
            selectedIndex={selectedMemoryIndex}
            loading={memoriesLoading}
          />
        )}
      </box>

      {/* Help bar */}
      <box height={1} width="100%">
        <text>
          {inputMode
            ? "Type query, Enter:submit, Escape:cancel, Backspace:delete"
            : "j/k:navigate  Tab:switch tab  /:search  m:mode  Enter:select  r:refresh  q:quit"}
        </text>
      </box>
    </box>
  );
}
