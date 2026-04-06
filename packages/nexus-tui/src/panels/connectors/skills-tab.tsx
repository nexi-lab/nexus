/**
 * Skills tab: view SKILL.md docs and browse operation schemas (read-only).
 *
 * Two view modes: "doc" shows SKILL.md content, "schema" shows annotated schema.
 * Select a mount first, then browse its skill docs and schemas.
 */

import { createEffect } from "solid-js";
import type { JSX } from "solid-js";
import type { FetchClient } from "@nexus-ai-fs/api-client";
import { useConnectorsStore } from "../../stores/connectors-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useCopy } from "../../shared/hooks/use-copy.js";
import { listNavigationBindings } from "../../shared/hooks/use-list-navigation.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";
import { statusColor } from "../../shared/theme.js";

interface SkillsTabProps {
  readonly client: FetchClient;
  readonly overlayActive: boolean;
}

export function SkillsTab({ client, overlayActive }: SkillsTabProps): JSX.Element {
  const mounts = useConnectorsStore((s) => s.mounts);
  const selectedMountIndex = useConnectorsStore((s) => s.selectedSkillMountIndex);
  const skillDoc = useConnectorsStore((s) => s.skillDoc);
  const skillDocLoading = useConnectorsStore((s) => s.skillDocLoading);
  const selectedSchemaIndex = useConnectorsStore((s) => s.selectedSchemaIndex);
  const schemaDoc = useConnectorsStore((s) => s.schemaDoc);
  const schemaDocLoading = useConnectorsStore((s) => s.schemaDocLoading);
  const viewMode = useConnectorsStore((s) => s.skillViewMode);

  const setSelectedMountIndex = useConnectorsStore((s) => s.setSelectedSkillMountIndex);
  const setSelectedSchemaIndex = useConnectorsStore((s) => s.setSelectedSchemaIndex);
  const setSkillViewMode = useConnectorsStore((s) => s.setSkillViewMode);
  const fetchSkillDoc = useConnectorsStore((s) => s.fetchSkillDoc);
  const fetchSchema = useConnectorsStore((s) => s.fetchSchema);
  const fetchMounts = useConnectorsStore((s) => s.fetchMounts);

  const { copy, copied } = useCopy();

  const selectedMount = mounts[selectedMountIndex];

  // Auto-fetch mounts if empty
  createEffect(() => {
    if (mounts.length === 0) {
      fetchMounts(client);
    }
  });

  // Auto-fetch skill doc when mount selection changes
  createEffect(() => {
    if (selectedMount && viewMode === "doc") {
      fetchSkillDoc(selectedMount.mount_point, client);
    }
  });

  // Fetch schema when schema selection changes
  const handleSchemaSelect = (index: number) => {
      if (!selectedMount || !skillDoc) return;
      const operation = skillDoc.schemas[index];
      if (operation) {
        setSelectedSchemaIndex(index);
        fetchSchema(selectedMount.mount_point, operation, client);
      }
    };

  // Build navigation bindings based on view mode
  const mountNav = listNavigationBindings({
    getIndex: () => selectedMountIndex,
    setIndex: (i) => {
      setSelectedMountIndex(i);
      setSelectedSchemaIndex(0);
    },
    getLength: () => mounts.length,
  });

  const schemaNav = listNavigationBindings({
    getIndex: () => selectedSchemaIndex,
    setIndex: setSelectedSchemaIndex,
    getLength: () => (skillDoc?.schemas.length ?? 0),
    onSelect: handleSchemaSelect,
  });

  useKeyboard(
    overlayActive
      ? {}
      : viewMode === "doc"
        ? {
            ...mountNav,
            s: () => setSkillViewMode("schema"),
            r: () => {
              if (selectedMount) fetchSkillDoc(selectedMount.mount_point, client);
            },
            y: () => {
              if (skillDoc?.content) copy(skillDoc.content);
            },
          }
        : {
            ...schemaNav,
            d: () => setSkillViewMode("doc"),
            r: () => {
              if (selectedMount && skillDoc) {
                const op = skillDoc.schemas[selectedSchemaIndex];
                if (op) fetchSchema(selectedMount.mount_point, op, client);
              }
            },
            y: () => {
              if (schemaDoc?.content) copy(schemaDoc.content);
            },
            escape: () => setSkillViewMode("doc"),
          },
  );

  return (
    <box flexDirection="column" height="100%" width="100%">
      {/* Mount selector (top row) */}
      <box height={1} width="100%">
        <text>
          <span foregroundColor={statusColor.dim}>Mount: </span>
          {mounts.length === 0 ? (
            <span foregroundColor={statusColor.dim}>No mounts</span>
          ) : (
            mounts.map((m, i) => (
              <span

                foregroundColor={i === selectedMountIndex ? statusColor.info : statusColor.dim}
                bold={i === selectedMountIndex}
              >
                {i === selectedMountIndex ? `[${m.mount_point}]` : ` ${m.mount_point} `}
              </span>
            ))
          )}
          <span foregroundColor={statusColor.dim}>{"  "}</span>
          <span foregroundColor={viewMode === "doc" ? statusColor.info : statusColor.dim}>
            {viewMode === "doc" ? "[Doc]" : " Doc "}
          </span>
          <span foregroundColor={viewMode === "schema" ? statusColor.info : statusColor.dim}>
            {viewMode === "schema" ? "[Schema]" : " Schema "}
          </span>
        </text>
      </box>

      {/* Content area */}
      <box flexGrow={1} borderStyle="single" marginTop={1} flexDirection="column">
        {viewMode === "doc" ? (
          // SKILL.md viewer
          skillDocLoading ? (
            <LoadingIndicator message="Loading skill doc..." />
          ) : skillDoc?.content ? (
            <box flexDirection="column" width="100%">
              {skillDoc.content.split("\n").slice(0, 30).map((line, i) => (
                <box height={1} width="100%">
                  <text>{line}</text>
                </box>
              ))}
              {skillDoc.content.split("\n").length > 30 && (
                <box height={1} width="100%">
                  <text foregroundColor={statusColor.dim}>... (truncated, press y to copy full doc)</text>
                </box>
              )}
            </box>
          ) : (
            <box height={1} width="100%">
              <text foregroundColor={statusColor.dim}>No skill doc available. Mount a connector with skill support.</text>
            </box>
          )
        ) : (
          // Schema browser
          <box flexDirection="row" height="100%" width="100%">
            {/* Schema list (left) */}
            <box width="30%" flexDirection="column" borderStyle="single">
              <box height={1} width="100%">
                <text bold foregroundColor={statusColor.info}>Operations</text>
              </box>
              {skillDoc?.schemas.map((op, i) => (
                <box height={1} width="100%">
                  <text>
                    <span foregroundColor={i === selectedSchemaIndex ? statusColor.info : undefined}>
                      {i === selectedSchemaIndex ? `▶ ${op}` : `  ${op}`}
                    </span>
                  </text>
                </box>
              ))}
              {(!skillDoc || skillDoc.schemas.length === 0) && (
                <box height={1} width="100%">
                  <text foregroundColor={statusColor.dim}>No schemas</text>
                </box>
              )}
            </box>

            {/* Schema content (right) */}
            <box width="70%" flexDirection="column" paddingLeft={1}>
              {schemaDocLoading ? (
                <LoadingIndicator message="Loading schema..." />
              ) : schemaDoc?.content ? (
                <box flexDirection="column" width="100%">
                  <box height={1} width="100%">
                    <text bold>{schemaDoc.operation}</text>
                  </box>
                  {schemaDoc.content.split("\n").slice(0, 25).map((line, i) => (
                    <box height={1} width="100%">
                      <text foregroundColor={statusColor.dim}>{line}</text>
                    </box>
                  ))}
                </box>
              ) : (
                <box height={1} width="100%">
                  <text foregroundColor={statusColor.dim}>Select an operation to view its schema.</text>
                </box>
              )}
            </box>
          </box>
        )}
      </box>

      {/* Help bar */}
      <box height={1} width="100%">
        {copied ? (
          <text foregroundColor={statusColor.healthy}>Copied!</text>
        ) : viewMode === "doc" ? (
          <text foregroundColor={statusColor.dim}>
            j/k:select mount  s:schemas  y:copy  r:refresh
          </text>
        ) : (
          <text foregroundColor={statusColor.dim}>
            j/k:select operation  Enter:view  d:doc view  y:copy  Esc:back  r:refresh
          </text>
        )}
      </box>
    </box>
  );
}
