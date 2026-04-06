/**
 * Stack panel: Docker container status, nexus.yaml config, .state.json runtime,
 * and server health — all in one place for debugging.
 *
 * Tabs: Containers | Config | State
 * Keybindings: Tab to switch, r to refresh, j/k to scroll.
 */

import { createEffect, createSignal } from "solid-js";
import type { JSX } from "solid-js";
import { useStackStore, type StackTab, type ContainerInfo, type StackPaths } from "../../stores/stack-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { useUiStore } from "../../stores/ui-store.js";
import { useGlobalStore } from "../../stores/global-store.js";
import { EmptyState } from "../../shared/components/empty-state.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";
import { statusColor } from "../../shared/theme.js";

// =============================================================================
// Tab definitions
// =============================================================================

const TAB_ORDER: readonly StackTab[] = ["containers", "config", "state"];

const TAB_LABELS: Readonly<Record<StackTab, string>> = {
  containers: "Containers",
  config: "Config",
  state: "State",
};

// =============================================================================
// Container status colors
// =============================================================================

const CONTAINER_STATE_COLOR: Record<string, string> = {
  running: statusColor.healthy,
  exited: statusColor.error,
  paused: statusColor.warning,
  restarting: statusColor.warning,
  dead: statusColor.error,
  created: statusColor.dim,
};

const HEALTH_COLOR: Record<string, string> = {
  healthy: statusColor.healthy,
  unhealthy: statusColor.error,
  starting: statusColor.warning,
};

// =============================================================================
// Sub-components
// =============================================================================

function ContainerList(props: {
  containers: readonly ContainerInfo[];
  loading: boolean;
  selectedIndex: number;
}): JSX.Element {
  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>
        {props.loading
          ? "Querying Docker..."
          : props.containers.length === 0
            ? "No containers found. Start the stack with: nexus up"
            : `${props.containers.length} containers`}
      </text>
      <scrollbox flexGrow={1} width="100%">
        {/* Header */}
        <box height={1} width="100%">
          <text>{"  CONTAINER NAME                      SERVICE       STATE       HEALTH      PORTS                    IMAGE"}</text>
        </box>
        <box height={1} width="100%">
          <text>{"  ------------------------------------  -----------  ----------  ----------  -----------------------  -------------------------"}</text>
        </box>

        {/* Rows */}
        {props.containers.map((c, i) => {
          const isSelected = i === props.selectedIndex;
          const prefix = isSelected ? "> " : "  ";
          const stateColor = CONTAINER_STATE_COLOR[c.state] ?? statusColor.dim;
          const hColor = HEALTH_COLOR[c.health] ?? statusColor.dim;
          const name = c.name.length > 36 ? c.name.slice(0, 33) + "..." : c.name;
          const image = c.image.length > 25 ? c.image.slice(0, 22) + "..." : c.image;
          const ports = c.ports.length > 23 ? c.ports.slice(0, 20) + "..." : c.ports;

          return (
            <box height={1} width="100%">
              <text>
                {`${prefix}${name.padEnd(36)}  ${c.service.padEnd(11)}  `}
                <span foregroundColor={stateColor}>{c.state.padEnd(10)}</span>
                {"  "}
                <span foregroundColor={hColor}>{(c.health || "-").padEnd(10)}</span>
                {`  ${ports.padEnd(23)}  ${image}`}
              </text>
            </box>
          );
        })}
      </scrollbox>
    </box>
  );
}

function ConfigView(props: {
  yaml: string;
  loading: boolean;
  scrollOffset: number;
}): JSX.Element {
  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>
        {props.loading
          ? "Reading nexus.yaml..."
          : !props.yaml
            ? "No nexus.yaml found. Run: nexus init --preset shared"
            : "nexus.yaml"}
      </text>

      {(() => {
        if (props.loading || !props.yaml) return null;
        const lines = props.yaml.split("\n");

        return (
          <scrollbox flexGrow={1} width="100%">
            <box height={1} width="100%">
              <text foregroundColor={statusColor.info}>{"  nexus.yaml"}</text>
            </box>
            <box height={1} width="100%">
              <text dimColor>{"  " + "─".repeat(60)}</text>
            </box>
            {lines.slice(props.scrollOffset).map((line, i) => (
              <box height={1} width="100%">
                <text>
                  <span dimColor>{`  ${String(props.scrollOffset + i + 1).padStart(3)}  `}</span>
                  {line}
                </text>
              </box>
            ))}
          </scrollbox>
        );
      })()}
    </box>
  );
}

function StateView(props: {
  stateJson: Record<string, unknown> | null;
  loading: boolean;
  projectName: string | null;
  scrollOffset: number;
}): JSX.Element {
  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>
        {props.loading
          ? "Reading .state.json..."
          : !props.stateJson
            ? "No .state.json found. Start the stack first."
            : ".state.json (runtime)"}
      </text>

      {(() => {
        if (props.loading || !props.stateJson) return null;

        // Render key-value pairs with nested object support
        const lines: { key: string; value: string; indent: number }[] = [];

        function flatten(obj: Record<string, unknown>, indent: number): void {
          for (const [key, val] of Object.entries(obj)) {
            if (val && typeof val === "object" && !Array.isArray(val)) {
              lines.push({ key, value: "", indent });
              flatten(val as Record<string, unknown>, indent + 1);
            } else {
              lines.push({ key, value: String(val), indent });
            }
          }
        }
        flatten(props.stateJson, 0);

        return (
          <scrollbox flexGrow={1} width="100%">
            <box height={1} width="100%">
              <text foregroundColor={statusColor.info}>{"  .state.json (runtime)"}</text>
            </box>
            {props.projectName && (
              <box height={1} width="100%">
                <text>
                  {"  project_name: "}
                  <span foregroundColor={statusColor.identity}>{props.projectName}</span>
                </text>
              </box>
            )}
            <box height={1} width="100%">
              <text dimColor>{"  " + "─".repeat(60)}</text>
            </box>
            {lines.slice(props.scrollOffset).map((line, i) => {
              const pad = "  ".repeat(line.indent);
              return (
                <box height={1} width="100%">
                  <text>
                    {"  "}{pad}
                    <span foregroundColor={statusColor.info}>{line.key}</span>
                    {line.value ? ": " : ""}
                    {line.value}
                  </text>
                </box>
              );
            })}
          </scrollbox>
        );
      })()}
    </box>
  );
}

function PathsBar({ paths }: { paths: StackPaths | null }): JSX.Element {
  if (!paths) return null;

  return (
    <box height={4} width="100%" flexDirection="column">
      <box height={1} width="100%">
        <text dimColor>{"  Paths:"}</text>
      </box>
      <box height={1} width="100%">
        <text>
          {"    nexus.yaml   "}
          <span foregroundColor={statusColor.reference}>{paths.nexusYaml}</span>
        </text>
      </box>
      <box height={1} width="100%">
        <text>
          {"    state.json   "}
          <span foregroundColor={statusColor.reference}>{paths.stateJson}</span>
        </text>
      </box>
      <box height={1} width="100%">
        <text>
          {"    compose      "}
          <span foregroundColor={statusColor.reference}>{paths.composeFile}</span>
          <span dimColor>{"  │  data: "}</span>
          <span foregroundColor={statusColor.reference}>{paths.dataDir}</span>
        </text>
      </box>
    </box>
  );
}

function HealthSummary({
  healthDetails,
  uptime,
  serverVersion,
}: {
  healthDetails: { status: string; components: Record<string, { status: string; detail?: string }> } | null;
  uptime: number | null;
  serverVersion: string | null;
}): JSX.Element {
  if (!healthDetails) return null;

  const color = healthDetails.status === "healthy"
    ? statusColor.healthy
    : healthDetails.status === "degraded"
    ? statusColor.warning
    : statusColor.error;

  const uptimeStr = uptime != null
    ? `${Math.floor(uptime / 3600)}h ${Math.floor((uptime % 3600) / 60)}m`
    : "-";

  const componentEntries = Object.entries(healthDetails.components);

  return (
    <box height={componentEntries.length > 0 ? componentEntries.length + 3 : 2} width="100%" flexDirection="column">
      <box height={1} width="100%">
        <text>
          {"  Server: "}
          <span foregroundColor={color}>{healthDetails.status}</span>
          {"  │  uptime: "}{uptimeStr}
          {serverVersion ? `  │  v${serverVersion}` : ""}
        </text>
      </box>
      {componentEntries.length > 0 && (
        <>
          <box height={1} width="100%">
            <text dimColor>{"  Components:"}</text>
          </box>
          {componentEntries.map(([name, comp]) => {
            const cColor = comp.status === "healthy" || comp.status === "ok"
              ? statusColor.healthy
              : comp.status === "degraded"
              ? statusColor.warning
              : statusColor.error;
            return (
              <box height={1} width="100%">
                <text>
                  {"    "}
                  <span foregroundColor={cColor}>{"●"}</span>
                  {` ${name.padEnd(24)} `}
                  <span foregroundColor={cColor}>{comp.status}</span>
                  {comp.detail ? `  ${comp.detail}` : ""}
                </text>
              </box>
            );
          })}
        </>
      )}
    </box>
  );
}

// =============================================================================
// Main panel
// =============================================================================

export default function StackPanel(): JSX.Element {
  const client = useApi();
  const overlayActive = () => useUiStore((s) => s.overlayActive);
  const serverVersion = () => useGlobalStore((s) => s.serverVersion);
  const uptime = () => useGlobalStore((s) => s.uptime);

  const activeTab = () => useStackStore((s) => s.activeTab);
  const setActiveTab = useStackStore((s) => s.setActiveTab);
  const containers = () => useStackStore((s) => s.containers);
  const containersLoading = () => useStackStore((s) => s.containersLoading);
  const configYaml = () => useStackStore((s) => s.configYaml);
  const configLoading = () => useStackStore((s) => s.configLoading);
  const stateJson = () => useStackStore((s) => s.stateJson);
  const stateLoading = () => useStackStore((s) => s.stateLoading);
  const healthDetails = () => useStackStore((s) => s.healthDetails);
  const paths = () => useStackStore((s) => s.paths);
  const error = () => useStackStore((s) => s.error);
  const refreshAll = useStackStore((s) => s.refreshAll);

  const [selectedIndex, setSelectedIndex] = createSignal(0);
  const [scrollOffset, setScrollOffset] = createSignal(0);

  // Tracks whether any fetch has ever started on this mount.
  // Set reactively when loading flags first go true — NOT at the call site —
  // so there is no render cycle where hasLoaded=true but loading=false and
  // data is still empty (which would flash misleading "no containers" state).
  const [hasLoaded, setHasLoaded] = createSignal(false);
  const anyLoading = () => containersLoading() || configLoading() || stateLoading();
  createEffect(() => {
    if (!hasLoaded() && anyLoading()) {
      setHasLoaded(true);
    }
  });

  // Derive project name from state.json
  const projectName = () => (stateJson()?.project_name as string | null) ?? null;

  // Auto-fetch on client connect so the panel surfaces real state on entry.
  // hasLoaded is set by the anyLoading effect above once refreshAll raises
  // loading flags — do NOT set it here to avoid the pre-load empty-state flash.
  createEffect(() => {
    if (client) {
      refreshAll(client);
    }
  });

  // Reset selection/scroll on tab change
  createEffect(() => {
    setSelectedIndex(0);
    setScrollOffset(0);
  });

  // List length for current tab (read fresh from store for keyboard handler)
  const listLength = (): number => {
    const s = useStackStore.getState();
    if (s.activeTab === "containers") return s.containers.length;
    if (s.activeTab === "config") return s.configYaml.split("\n").length;
    return s.stateJson ? Object.keys(s.stateJson).length * 3 : 0;
  };

  useKeyboard(
    (): Record<string, () => void> => {
      if (useUiStore.getState().overlayActive) return {};
      return {
          tab: () => {
            const tab = useStackStore.getState().activeTab;
            const idx = TAB_ORDER.indexOf(tab);
            const next = TAB_ORDER[(idx + 1) % TAB_ORDER.length]!;
            setActiveTab(next);
          },
          j: () => {
            const tab = useStackStore.getState().activeTab;
            if (tab === "containers") {
              setSelectedIndex((i) => Math.min(i + 1, useStackStore.getState().containers.length - 1));
            } else {
              setScrollOffset((o) => Math.min(o + 1, Math.max(listLength() - 5, 0)));
            }
          },
          down: () => {
            const tab = useStackStore.getState().activeTab;
            if (tab === "containers") {
              setSelectedIndex((i) => Math.min(i + 1, useStackStore.getState().containers.length - 1));
            } else {
              setScrollOffset((o) => Math.min(o + 1, Math.max(listLength() - 5, 0)));
            }
          },
          k: () => {
            const tab = useStackStore.getState().activeTab;
            if (tab === "containers") {
              setSelectedIndex((i) => Math.max(i - 1, 0));
            } else {
              setScrollOffset((o) => Math.max(o - 1, 0));
            }
          },
          up: () => {
            const tab = useStackStore.getState().activeTab;
            if (tab === "containers") {
              setSelectedIndex((i) => Math.max(i - 1, 0));
            } else {
              setScrollOffset((o) => Math.max(o - 1, 0));
            }
          },
          r: () => {
            refreshAll(client);
          },
          g: () => {
            setSelectedIndex(0);
            setScrollOffset(0);
          },
          "shift+g": () => {
            const tab = useStackStore.getState().activeTab;
            if (tab === "containers") {
              setSelectedIndex(Math.max(useStackStore.getState().containers.length - 1, 0));
            } else {
              setScrollOffset(Math.max(listLength() - 5, 0));
            }
          },
        };
    },
  );

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Tab bar */}
      <box height={1} width="100%">
        <text>
          {TAB_ORDER.map((tab) => {
            const label = TAB_LABELS[tab];
            return tab === activeTab() ? `[${label}]` : ` ${label} `;
          }).join(" ")}
        </text>
      </box>

      {/* Error display */}
      {error() && (
        <box height={1} width="100%">
          <text foregroundColor={statusColor.error}>{`  Error: ${error()}`}</text>
        </box>
      )}

      {/* Health summary (always visible) */}
      <HealthSummary
        healthDetails={healthDetails()}
        uptime={uptime()}
        serverVersion={serverVersion()}
      />

      {/* File paths */}
      <PathsBar paths={paths()} />

      {/* Main content */}
      <box flexGrow={1} borderStyle="single">
        {!hasLoaded() && !containersLoading() && !configLoading() && !stateLoading() && (
          <box height="100%" width="100%" justifyContent="center" alignItems="center">
            <text dimColor>{"  Press r to load stack info"}</text>
          </box>
        )}
        {(hasLoaded() || containersLoading() || configLoading() || stateLoading()) && activeTab() === "containers" && (
          <ContainerList
            containers={containers()}
            loading={containersLoading()}
            selectedIndex={selectedIndex()}
          />
        )}
        {(hasLoaded() || configLoading()) && activeTab() === "config" && (
          <ConfigView
            yaml={configYaml()}
            loading={configLoading()}
            scrollOffset={scrollOffset()}
          />
        )}
        {(hasLoaded() || stateLoading()) && activeTab() === "state" && (
          <StateView
            stateJson={stateJson()}
            loading={stateLoading()}
            projectName={projectName()}
            scrollOffset={scrollOffset()}
          />
        )}
      </box>

      {/* Help bar */}
      <box height={1} width="100%">
        <text dimColor>
          {"  j/k:navigate  Tab:switch tab  r:refresh  g/G:top/bottom  q:quit"}
        </text>
      </box>
    </box>
  );
}
