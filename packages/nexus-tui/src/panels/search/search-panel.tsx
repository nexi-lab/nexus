/**
 * Search & Knowledge panel: tabbed layout with search, knowledge graph,
 * and memories views.
 *
 * Press / to enter search input mode, type query, Enter to submit, Escape to cancel.
 */

import React, { useState, useCallback, useEffect } from "react";
import { useSearchStore } from "../../stores/search-store.js";
import { useGlobalStore } from "../../stores/global-store.js";
import type { SearchTab, SearchMode } from "../../stores/search-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { useVisibleTabs, type TabDef } from "../../shared/hooks/use-visible-tabs.js";
import { SearchResults } from "./search-results.js";
import { KnowledgeView } from "./knowledge-view.js";
import { MemoryList } from "./memory-list.js";
import { PlaybookList } from "./playbook-list.js";
import { RlmAnswerView } from "./rlm-answer-view.js";
import { ColumnSearch } from "./column-search.js";
import { useKnowledgeStore } from "../../stores/knowledge-store.js";

const ALL_TABS: readonly TabDef<SearchTab>[] = [
  { id: "search", label: "Search", brick: "search" },
  { id: "knowledge", label: "Knowledge", brick: "catalog" },
  { id: "memories", label: "Memories", brick: "memory" },
  { id: "playbooks", label: "Playbooks", brick: null },
  { id: "ask", label: "Ask", brick: "rlm" },
  { id: "columns", label: "Columns", brick: "catalog" },
];
const TAB_LABELS: Readonly<Record<SearchTab, string>> = {
  search: "Search",
  knowledge: "Knowledge",
  memories: "Memories",
  playbooks: "Playbooks",
  ask: "Ask",
  columns: "Columns",
};

const MODE_LABELS: Readonly<Record<SearchMode, string>> = {
  keyword: "KW",
  semantic: "SEM",
  hybrid: "HYB",
};

export default function SearchPanel(): React.ReactNode {
  const client = useApi();
  const visibleTabs = useVisibleTabs(ALL_TABS);
  // Effective zone: explicit config > server-discovered zone (matches status-bar fallback)
  const configZoneId = useGlobalStore((s) => s.config.zoneId);
  const serverZoneId = useGlobalStore((s) => s.zoneId);
  const effectiveZoneId = configZoneId ?? serverZoneId ?? undefined;
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
  const memoryHistory = useSearchStore((s) => s.memoryHistory);
  const memoryHistoryLoading = useSearchStore((s) => s.memoryHistoryLoading);
  const memoryDiff = useSearchStore((s) => s.memoryDiff);
  const memoryDiffLoading = useSearchStore((s) => s.memoryDiffLoading);
  const playbooks = useSearchStore((s) => s.playbooks);
  const selectedPlaybookIndex = useSearchStore((s) => s.selectedPlaybookIndex);
  const playbooksLoading = useSearchStore((s) => s.playbooksLoading);
  const rlmAnswer = useSearchStore((s) => s.rlmAnswer);
  const rlmLoading = useSearchStore((s) => s.rlmLoading);
  const rlmContextPaths = useSearchStore((s) => s.rlmContextPaths);
  const activeTab = useSearchStore((s) => s.activeTab);
  const error = useSearchStore((s) => s.error);

  // Knowledge store (column search)
  const columnSearchResults = useKnowledgeStore((s) => s.columnSearchResults);
  const columnSearchLoading = useKnowledgeStore((s) => s.columnSearchLoading);
  const searchByColumn = useKnowledgeStore((s) => s.searchByColumn);

  const searchMode = useSearchStore((s) => s.searchMode);
  const cycleSearchMode = useSearchStore((s) => s.cycleSearchMode);

  const search = useSearchStore((s) => s.search);
  const fetchEntity = useSearchStore((s) => s.fetchEntity);
  const fetchNeighbors = useSearchStore((s) => s.fetchNeighbors);
  const searchKnowledge = useSearchStore((s) => s.searchKnowledge);
  const fetchMemories = useSearchStore((s) => s.fetchMemories);
  const fetchPlaybooks = useSearchStore((s) => s.fetchPlaybooks);
  const deletePlaybook = useSearchStore((s) => s.deletePlaybook);
  const deleteMemory = useSearchStore((s) => s.deleteMemory);
  const createMemory = useSearchStore((s) => s.createMemory);
  const updateMemory = useSearchStore((s) => s.updateMemory);
  const setSelectedPlaybookIndex = useSearchStore((s) => s.setSelectedPlaybookIndex);
  const askRlm = useSearchStore((s) => s.askRlm);
  const addRlmContextPath = useSearchStore((s) => s.addRlmContextPath);
  const clearRlmContextPaths = useSearchStore((s) => s.clearRlmContextPaths);
  const fetchMemoryHistory = useSearchStore((s) => s.fetchMemoryHistory);
  const fetchMemoryDiff = useSearchStore((s) => s.fetchMemoryDiff);
  const clearMemoryHistory = useSearchStore((s) => s.clearMemoryHistory);
  const clearMemoryDiff = useSearchStore((s) => s.clearMemoryDiff);
  const setActiveTab = useSearchStore((s) => s.setActiveTab);
  const setSelectedResultIndex = useSearchStore((s) => s.setSelectedResultIndex);
  const setSelectedMemoryIndex = useSearchStore((s) => s.setSelectedMemoryIndex);
  const setSearchQuery = useSearchStore((s) => s.setSearchQuery);

  // Fall back to first visible tab if the active tab becomes hidden
  const visibleIds = visibleTabs.map((t) => t.id);
  useEffect(() => {
    if (visibleIds.length > 0 && !visibleIds.includes(activeTab)) {
      setActiveTab(visibleIds[0]!);
    }
  }, [visibleIds.join(","), activeTab, setActiveTab]);

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
      } else if (activeTab === "playbooks") {
        fetchPlaybooks(query.trim(), client);
      } else if (activeTab === "ask") {
        askRlm(query.trim(), client, effectiveZoneId);
      } else if (activeTab === "columns") {
        void searchByColumn(query.trim(), client);
      }
    },
    [client, activeTab, search, searchKnowledge, fetchMemories, fetchPlaybooks, askRlm, searchByColumn, setSearchQuery, effectiveZoneId],
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
    } else if (activeTab === "playbooks") {
      fetchPlaybooks(searchQuery || "", client);
    } else if (activeTab === "ask" && searchQuery) {
      askRlm(searchQuery, client, effectiveZoneId);
    } else if (activeTab === "columns" && searchQuery) {
      void searchByColumn(searchQuery, client);
    }
  }, [client, activeTab, searchQuery, search, searchKnowledge, fetchMemories, fetchPlaybooks, askRlm, searchByColumn, effectiveZoneId]);

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
            } else if (activeTab === "playbooks") {
              setSelectedPlaybookIndex(
                Math.min(selectedPlaybookIndex + 1, playbooks.length - 1),
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
            } else if (activeTab === "playbooks") {
              setSelectedPlaybookIndex(
                Math.min(selectedPlaybookIndex + 1, playbooks.length - 1),
              );
            }
          },
          k: () => {
            if (activeTab === "search") {
              setSelectedResultIndex(Math.max(selectedResultIndex - 1, 0));
            } else if (activeTab === "memories") {
              setSelectedMemoryIndex(Math.max(selectedMemoryIndex - 1, 0));
            } else if (activeTab === "playbooks") {
              setSelectedPlaybookIndex(Math.max(selectedPlaybookIndex - 1, 0));
            }
          },
          up: () => {
            if (activeTab === "search") {
              setSelectedResultIndex(Math.max(selectedResultIndex - 1, 0));
            } else if (activeTab === "memories") {
              setSelectedMemoryIndex(Math.max(selectedMemoryIndex - 1, 0));
            } else if (activeTab === "playbooks") {
              setSelectedPlaybookIndex(Math.max(selectedPlaybookIndex - 1, 0));
            }
          },
          tab: () => {
            const ids = visibleTabs.map((t) => t.id);
            const currentIdx = ids.indexOf(activeTab);
            const nextIdx = (currentIdx + 1) % ids.length;
            const nextTab = ids[nextIdx];
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
            } else if (activeTab === "memories") {
              const memory = memories[selectedMemoryIndex];
              if (memory) {
                const memId = String(
                  (memory as Record<string, unknown>).memory_id ?? "",
                );
                if (memId) {
                  // Toggle: if history is already shown for this memory, clear it
                  if (memoryHistory?.memory_id === memId) {
                    clearMemoryHistory();
                    clearMemoryDiff();
                  } else {
                    clearMemoryDiff();
                    fetchMemoryHistory(memId, client);
                  }
                }
              }
            }
          },
          v: () => {
            if (!client || activeTab !== "memories") return;

            const memory = memories[selectedMemoryIndex];
            if (!memory) return;

            const memId = String(
              (memory as Record<string, unknown>).memory_id ?? "",
            );
            if (!memId) return;

            // Need at least 2 versions to diff
            if (memoryHistory && memoryHistory.memory_id === memId && memoryHistory.current_version >= 2) {
              fetchMemoryDiff(
                memId,
                memoryHistory.current_version - 1,
                memoryHistory.current_version,
                client,
              );
            }
          },
          d: () => {
            if (!client) return;
            if (activeTab === "playbooks") {
              const playbook = playbooks[selectedPlaybookIndex];
              if (playbook) {
                deletePlaybook(playbook.playbook_id, client);
              }
            } else if (activeTab === "memories") {
              const memory = memories[selectedMemoryIndex];
              if (memory) {
                const memId = String((memory as Record<string, unknown>).memory_id ?? "");
                if (memId) deleteMemory(memId, client);
              }
            }
          },
          n: () => {
            // Create new memory from search query text
            if (activeTab === "memories" && client && searchQuery.trim()) {
              createMemory(searchQuery.trim(), {}, client);
            }
          },
          u: () => {
            // Update selected memory with current search query as new content
            if (activeTab === "memories" && client && searchQuery.trim()) {
              const memory = memories[selectedMemoryIndex];
              if (memory) {
                const memId = String((memory as Record<string, unknown>).memory_id ?? "");
                if (memId) updateMemory(memId, searchQuery.trim(), client);
              }
            }
          },
          a: () => {
            // Add selected search result path to RLM document context
            if (activeTab === "search") {
              const result = searchResults[selectedResultIndex];
              if (result) {
                addRlmContextPath(result.path);
              }
            } else if (activeTab === "ask") {
              // Clear context paths when pressing 'a' on Ask tab
              clearRlmContextPaths();
            }
          },
          escape: () => {
            // Clear expanded views when Escape is pressed in normal mode
            if (activeTab === "memories" && (memoryHistory || memoryDiff)) {
              clearMemoryHistory();
              clearMemoryDiff();
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
          {visibleTabs.map((tab) => {
            return tab.id === activeTab ? `[${tab.label}]` : ` ${tab.label} `;
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
            memoryHistory={memoryHistory}
            memoryHistoryLoading={memoryHistoryLoading}
            memoryDiff={memoryDiff}
            memoryDiffLoading={memoryDiffLoading}
          />
        )}
        {activeTab === "playbooks" && (
          <PlaybookList
            playbooks={playbooks}
            selectedIndex={selectedPlaybookIndex}
            loading={playbooksLoading}
          />
        )}
        {activeTab === "ask" && (
          <RlmAnswerView answer={rlmAnswer} loading={rlmLoading} contextPaths={rlmContextPaths} />
        )}
        {activeTab === "columns" && (
          <ColumnSearch results={columnSearchResults} loading={columnSearchLoading} />
        )}
      </box>

      {/* Help bar */}
      <box height={1} width="100%">
        <text>
          {inputMode
            ? "Type query, Enter:submit, Escape:cancel, Backspace:delete"
            : activeTab === "memories"
              ? "j/k:navigate  Tab:tab  /:search  Enter:history  v:diff  n:create  u:update  d:delete  Esc:close  r:refresh  q:quit"
              : activeTab === "ask"
                ? "/:ask  a:clear context  Tab:switch tab  r:refresh  q:quit"
                : activeTab === "columns"
                  ? "/:search column  Tab:switch tab  r:refresh  q:quit"
                  : activeTab === "search"
                    ? "j/k:navigate  a:add to context  /:search  m:mode  Enter:select  Tab:tab  r:refresh  q:quit"
                    : "j/k:navigate  Tab:switch tab  /:search  m:mode  Enter:select  d:delete  r:refresh  q:quit"}
        </text>
      </box>
    </box>
  );
}
