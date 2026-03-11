/**
 * Search & Knowledge panel: tabbed layout with search, knowledge graph,
 * memories, and playbooks views.
 */

import React, { useEffect } from "react";
import { useSearchStore } from "../../stores/search-store.js";
import type { SearchTab } from "../../stores/search-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { SearchResults } from "./search-results.js";
import { KnowledgeView } from "./knowledge-view.js";
import { MemoryList } from "./memory-list.js";
import { PlaybookList } from "./playbook-list.js";

const TAB_ORDER: readonly SearchTab[] = ["search", "knowledge", "memories", "playbooks"];
const TAB_LABELS: Readonly<Record<SearchTab, string>> = {
  search: "Search",
  knowledge: "Knowledge",
  memories: "Memories",
  playbooks: "Playbooks",
};

export default function SearchPanel(): React.ReactNode {
  const client = useApi();

  const searchQuery = useSearchStore((s) => s.searchQuery);
  const searchResults = useSearchStore((s) => s.searchResults);
  const searchTotal = useSearchStore((s) => s.searchTotal);
  const selectedResultIndex = useSearchStore((s) => s.selectedResultIndex);
  const searchLoading = useSearchStore((s) => s.searchLoading);
  const entities = useSearchStore((s) => s.entities);
  const selectedEntity = useSearchStore((s) => s.selectedEntity);
  const neighbors = useSearchStore((s) => s.neighbors);
  const knowledgeLoading = useSearchStore((s) => s.knowledgeLoading);
  const memories = useSearchStore((s) => s.memories);
  const selectedMemoryIndex = useSearchStore((s) => s.selectedMemoryIndex);
  const memoriesLoading = useSearchStore((s) => s.memoriesLoading);
  const playbooks = useSearchStore((s) => s.playbooks);
  const playbooksLoading = useSearchStore((s) => s.playbooksLoading);
  const activeTab = useSearchStore((s) => s.activeTab);
  const error = useSearchStore((s) => s.error);

  const search = useSearchStore((s) => s.search);
  const fetchEntity = useSearchStore((s) => s.fetchEntity);
  const fetchNeighbors = useSearchStore((s) => s.fetchNeighbors);
  const searchKnowledge = useSearchStore((s) => s.searchKnowledge);
  const fetchMemories = useSearchStore((s) => s.fetchMemories);
  const fetchPlaybooks = useSearchStore((s) => s.fetchPlaybooks);
  const setActiveTab = useSearchStore((s) => s.setActiveTab);
  const setSelectedResultIndex = useSearchStore((s) => s.setSelectedResultIndex);
  const setSelectedMemoryIndex = useSearchStore((s) => s.setSelectedMemoryIndex);
  const setSearchQuery = useSearchStore((s) => s.setSearchQuery);

  // Refresh current view based on active tab
  const refreshCurrentView = (): void => {
    if (!client) return;

    if (activeTab === "search" && searchQuery) {
      search(searchQuery, client);
    } else if (activeTab === "knowledge" && searchQuery) {
      searchKnowledge(searchQuery, client);
    } else if (activeTab === "memories") {
      fetchMemories(client);
    } else if (activeTab === "playbooks") {
      fetchPlaybooks(client);
    }
  };

  // Auto-fetch when tab changes
  useEffect(() => {
    refreshCurrentView();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, client]);

  useKeyboard({
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
    "/": () => {
      // Prompt for search query — in TUI context this sets a placeholder
      // The actual input is handled by the parent shell; here we signal intent.
      setSearchQuery("");
    },
    return: () => {
      if (!client) return;

      if (activeTab === "search") {
        const result = searchResults[selectedResultIndex];
        if (result) {
          fetchEntity(result.id, client);
          fetchNeighbors(result.id, client);
          setActiveTab("knowledge");
        }
      } else if (activeTab === "knowledge") {
        if (selectedEntity) {
          fetchNeighbors(selectedEntity.entity_id, client);
        }
      }
    },
  });

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Search query display */}
      <box height={1} width="100%">
        <text>{`Query: ${searchQuery || "(press / to search)"}`}</text>
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
            entities={entities}
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
        {activeTab === "playbooks" && (
          <PlaybookList
            playbooks={playbooks}
            loading={playbooksLoading}
          />
        )}
      </box>

      {/* Help bar */}
      <box height={1} width="100%">
        <text>
          {"j/k:navigate  Tab:switch tab  /:search  Enter:select  r:refresh  q:quit"}
        </text>
      </box>
    </box>
  );
}
