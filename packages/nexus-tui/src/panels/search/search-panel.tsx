/**
 * Search & Knowledge panel: tabbed layout with search, knowledge graph,
 * and memories views.
 *
 * Press / to enter search input mode, type query, Enter to submit, Escape to cancel.
 */

import React, { useCallback } from "react";
import { useSearchStore } from "../../stores/search-store.js";
import { useGlobalStore } from "../../stores/global-store.js";
import type { SearchMode } from "../../stores/search-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { listNavigationBindings } from "../../shared/hooks/use-list-navigation.js";
import { useTextInput } from "../../shared/hooks/use-text-input.js";
import { useConfirmStore } from "../../shared/hooks/use-confirm.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { useUiStore } from "../../stores/ui-store.js";
import { useVisibleTabs } from "../../shared/hooks/use-visible-tabs.js";
import { SubTabBar } from "../../shared/components/sub-tab-bar.js";
import { subTabCycleBindings } from "../../shared/components/sub-tab-bar-utils.js";
import { useTabFallback } from "../../shared/hooks/use-tab-fallback.js";
import { SearchResults } from "./search-results.js";
import { KnowledgeView } from "./knowledge-view.js";
import { MemoryList } from "./memory-list.js";
import { PlaybookList } from "./playbook-list.js";
import { RlmAnswerView } from "./rlm-answer-view.js";
import { ColumnSearch } from "./column-search.js";
import { useKnowledgeStore } from "../../stores/knowledge-store.js";
import { Tooltip } from "../../shared/components/tooltip.js";
import { SEARCH_TABS } from "../../shared/navigation.js";

const MODE_LABELS: Readonly<Record<SearchMode, string>> = {
  keyword: "KW",
  semantic: "SEM",
  hybrid: "HYB",
};

const HELP_TEXT: Readonly<Record<string, string>> = {
  search: "j/k:navigate  a:add to context  /:search  m:mode  Enter:select  Tab:tab  r:refresh  q:quit",
  knowledge: "j/k:navigate  Tab:switch tab  /:search  m:mode  Enter:select  d:delete  r:refresh  q:quit",
  memories: "j/k:navigate  Tab:tab  /:search  Enter:history  v:diff  n:create  u:update  d:delete  Esc:close  r:refresh  q:quit",
  playbooks: "j/k:navigate  Tab:switch tab  /:search  m:mode  Enter:select  d:delete  r:refresh  q:quit",
  ask: "/:ask  a:clear context  Tab:switch tab  r:refresh  q:quit",
  columns: "/:search column  Tab:switch tab  r:refresh  q:quit",
};

export default function SearchPanel(): React.ReactNode {
  const client = useApi();
  const confirm = useConfirmStore((s) => s.confirm);
  const overlayActive = useUiStore((s) => s.overlayActive);
  const visibleTabs = useVisibleTabs(SEARCH_TABS);
  // Effective zone: explicit config > server-discovered zone (matches status-bar fallback)
  const configZoneId = useGlobalStore((s) => s.config.zoneId);
  const serverZoneId = useGlobalStore((s) => s.zoneId);
  const effectiveZoneId = configZoneId ?? serverZoneId ?? undefined;

  const searchQuery = useSearchStore((s) => s.searchQuery);
  const searchResults = useSearchStore((s) => s.searchResults);
  const expandedContent = useSearchStore((s) => s.expandedContent);
  const expandedPath = useSearchStore((s) => s.expandedPath);
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

  useTabFallback(visibleTabs, activeTab, setActiveTab);

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

  // Text input for search bar
  const textInput = useTextInput({
    onSubmit: (val) => {
      // Clear expanded content when submitting a new search
      useSearchStore.setState({ expandedContent: null, expandedPath: null });
      submitSearch(val);
    },
  });

  // Shared list navigation (j/k/up/down/g/G) — switches per active tab
  const listNav = listNavigationBindings({
    getIndex: () => {
      if (activeTab === "search") return selectedResultIndex;
      if (activeTab === "memories") return selectedMemoryIndex;
      if (activeTab === "playbooks") return selectedPlaybookIndex;
      return 0;
    },
    setIndex: (i) => {
      if (activeTab === "search") setSelectedResultIndex(i);
      else if (activeTab === "memories") setSelectedMemoryIndex(i);
      else if (activeTab === "playbooks") setSelectedPlaybookIndex(i);
    },
    getLength: () => {
      if (activeTab === "search") return searchResults.length;
      if (activeTab === "memories") return memories.length;
      if (activeTab === "playbooks") return playbooks.length;
      return 0;
    },
  });

  useKeyboard(
    overlayActive
      ? {}
      : textInput.active
      ? textInput.inputBindings
      : {
          ...listNav,
          ...subTabCycleBindings(visibleTabs, activeTab, setActiveTab),
          r: () => refreshCurrentView(),
          m: () => cycleSearchMode(),
          "/": () => textInput.activate(searchQuery),
          return: () => {
            if (!client) return;

            if (activeTab === "search") {
              const result = searchResults[selectedResultIndex];
              if (result) {
                // Read the full file and show as expanded content
                client.get<{ content: string }>(`/api/v2/files/read?path=${encodeURIComponent(result.path)}`)
                  .then((r) => {
                    useSearchStore.setState({ expandedContent: r.content, expandedPath: result.path });
                  })
                  .catch(() => {});
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

            if (memoryHistory && memoryHistory.memory_id === memId && memoryHistory.current_version >= 2) {
              fetchMemoryDiff(
                memId,
                memoryHistory.current_version - 1,
                memoryHistory.current_version,
                client,
              );
            }
          },
          d: async () => {
            if (!client) return;
            if (activeTab === "playbooks") {
              const playbook = playbooks[selectedPlaybookIndex];
              if (playbook) {
                const ok = await confirm("Delete playbook?", "This cannot be undone.");
                if (!ok) return;
                deletePlaybook(playbook.playbook_id, client);
              }
            } else if (activeTab === "memories") {
              const memory = memories[selectedMemoryIndex];
              if (memory) {
                const memId = String((memory as Record<string, unknown>).memory_id ?? "");
                if (memId) {
                  const ok = await confirm("Delete memory?", "This cannot be undone.");
                  if (!ok) return;
                  deleteMemory(memId, client);
                }
              }
            }
          },
          n: () => {
            if (activeTab === "memories" && client && searchQuery.trim()) {
              createMemory(searchQuery.trim(), {}, client);
            }
          },
          u: () => {
            if (activeTab === "memories" && client && searchQuery.trim()) {
              const memory = memories[selectedMemoryIndex];
              if (memory) {
                const memId = String((memory as Record<string, unknown>).memory_id ?? "");
                if (memId) updateMemory(memId, searchQuery.trim(), client);
              }
            }
          },
          a: () => {
            if (activeTab === "search") {
              const result = searchResults[selectedResultIndex];
              if (result) {
                addRlmContextPath(result.path);
              }
            } else if (activeTab === "ask") {
              clearRlmContextPaths();
            }
          },
          escape: () => {
            // Close expanded file content
            if (expandedContent !== null) {
              useSearchStore.setState({ expandedContent: null, expandedPath: null });
              return;
            }
            if (activeTab === "memories" && (memoryHistory || memoryDiff)) {
              clearMemoryHistory();
              clearMemoryDiff();
            }
          },
        },
    overlayActive ? undefined : textInput.active ? textInput.onUnhandled : undefined,
  );

  return (
    <box height="100%" width="100%" flexDirection="column">
      <Tooltip tooltipKey="search-panel" message="Tip: Press ? for keybinding help" />
      {/* Search input bar */}
      <box height={1} width="100%">
        <text>
          {textInput.active
            ? `Search: ${textInput.buffer}█`
            : `Query: ${searchQuery || "(press / to search)"}  [${MODE_LABELS[searchMode]}]`}
        </text>
      </box>

      {/* Tab bar */}
      <SubTabBar tabs={visibleTabs} activeTab={activeTab} onSelect={setActiveTab as (id: string) => void} />

      {/* Error display */}
      {error && (
        <box height={1} width="100%">
          <text>{`Error: ${error}`}</text>
        </box>
      )}

      {/* Tab content */}
      <box flexGrow={1} borderStyle="single">
        {activeTab === "search" && expandedContent !== null && (
          <box height="100%" width="100%" flexDirection="column">
            <box height={1} width="100%">
              <text bold>{`── ${expandedPath} ── (Escape to close)`}</text>
            </box>
            <scrollbox flexGrow={1} width="100%">
              <text>{expandedContent}</text>
            </scrollbox>
          </box>
        )}
        {activeTab === "search" && expandedContent === null && (
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
          {textInput.active
            ? "Type query, Enter:submit, Escape:cancel, Backspace:delete"
            : HELP_TEXT[activeTab] ?? "j/k:navigate  Tab:switch tab  r:refresh  q:quit"}
        </text>
      </box>
    </box>
  );
}
