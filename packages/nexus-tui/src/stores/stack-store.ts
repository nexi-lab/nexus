/**
 * Zustand store for the Stack panel: Docker containers, nexus.yaml config,
 * .state.json runtime state, and server health details.
 *
 * Reads local files via Bun.file() and runs `docker compose ps` via Bun.spawn().
 * Server health is fetched via the FetchClient from the global store.
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";
import { useErrorStore } from "./error-store.js";
import { categorizeError } from "./create-api-action.js";

// =============================================================================
// Types
// =============================================================================

export interface ContainerInfo {
  readonly name: string;
  readonly service: string;
  readonly state: string;
  readonly health: string;
  readonly ports: string;
  readonly image: string;
}

export interface HealthComponent {
  readonly name: string;
  readonly status: string;
  readonly detail: string;
}

export interface DetailedHealth {
  readonly status: string;
  readonly service: string;
  readonly components: Record<string, { status: string; detail?: string }>;
}

export type StackTab = "containers" | "config" | "state";

/** Paths to config/state files used by the stack. */
export interface StackPaths {
  readonly projectRoot: string;
  readonly nexusYaml: string;
  readonly stateJson: string;
  readonly composeFile: string;
  readonly dataDir: string;
}

export interface StackState {
  // Tabs
  readonly activeTab: StackTab;

  // Docker containers
  readonly containers: readonly ContainerInfo[];
  readonly containersLoading: boolean;

  // nexus.yaml raw content
  readonly configYaml: string;
  readonly configLoading: boolean;

  // .state.json parsed
  readonly stateJson: Record<string, unknown> | null;
  readonly stateLoading: boolean;

  // Server health details
  readonly healthDetails: DetailedHealth | null;
  readonly healthLoading: boolean;

  // File paths
  readonly paths: StackPaths | null;

  // General
  readonly error: string | null;
  readonly lastRefreshed: number;

  // Actions
  readonly setActiveTab: (tab: StackTab) => void;
  readonly fetchContainers: () => Promise<void>;
  readonly fetchConfig: () => Promise<void>;
  readonly fetchState: () => Promise<void>;
  readonly fetchHealth: (client: FetchClient) => Promise<void>;
  readonly refreshAll: (client: FetchClient | null) => Promise<void>;
}

// =============================================================================
// Helpers
// =============================================================================

/**
 * Find the project root by walking up from CWD looking for nexus.yaml.
 * Walks up to 20 levels to handle git worktrees nested inside the main repo
 * (e.g. .claude/worktrees/<name>/packages/nexus-tui/).
 * Returns CWD if not found (commands will just fail gracefully).
 */
function findProjectRoot(): string {
  const path = require("node:path");
  const fs = require("node:fs");
  let dir = process.cwd();
  for (let i = 0; i < 20; i++) {
    if (fs.existsSync(path.join(dir, "nexus.yaml"))) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return process.cwd();
}

/**
 * Deduplicate port mappings from Docker Publishers array.
 * Docker lists each mapping twice (IPv4 0.0.0.0 + IPv6 ::).
 * Only show published (host-mapped) ports, skip unexposed ones.
 */
function formatPorts(publishers: { URL?: string; PublishedPort?: number; TargetPort?: number }[]): string {
  const seen = new Set<string>();
  const parts: string[] = [];
  for (const p of publishers) {
    if (!p.PublishedPort) continue; // skip unexposed ports
    const key = `${p.PublishedPort}->${p.TargetPort}`;
    if (seen.has(key)) continue;
    seen.add(key);
    parts.push(key);
  }
  return parts.join(", ");
}

function containerFromObj(obj: Record<string, unknown>): ContainerInfo {
  const publishers = obj.Publishers;
  return {
    name: (obj.Name ?? obj.Names ?? "") as string,
    service: (obj.Service ?? "") as string,
    state: (obj.State ?? "") as string,
    health: (obj.Health ?? "") as string,
    ports: Array.isArray(publishers)
      ? formatPorts(publishers as { URL?: string; PublishedPort?: number; TargetPort?: number }[])
      : (obj.Ports ?? "") as string,
    image: (obj.Image ?? "") as string,
  };
}

/**
 * Parse `docker compose ps --format json` output.
 * Handles both formats:
 *   - NDJSON (one JSON object per line) — newer Compose versions
 *   - JSON array (single `[...]` blob) — older Compose versions
 */
function parseDockerPs(output: string): ContainerInfo[] {
  const trimmed = output.trim();
  if (!trimmed) return [];

  // Try JSON array first (older Compose: single [...] output)
  if (trimmed.startsWith("[")) {
    try {
      const arr = JSON.parse(trimmed);
      if (Array.isArray(arr)) {
        return arr.map(containerFromObj);
      }
    } catch {
      // Fall through to NDJSON parsing
    }
  }

  // NDJSON: one JSON object per line (newer Compose)
  const containers: ContainerInfo[] = [];
  for (const line of trimmed.split("\n")) {
    const l = line.trim();
    if (!l || !l.startsWith("{")) continue;
    try {
      containers.push(containerFromObj(JSON.parse(l)));
    } catch {
      // Skip non-JSON lines
    }
  }
  return containers;
}

// =============================================================================
// Store
// =============================================================================

export const useStackStore = create<StackState>((set, get) => ({
  activeTab: "containers",
  containers: [],
  containersLoading: false,
  configYaml: "",
  configLoading: false,
  stateJson: null,
  stateLoading: false,
  healthDetails: null,
  healthLoading: false,
  paths: null,
  error: null,
  lastRefreshed: 0,

  setActiveTab: (tab) => set({ activeTab: tab }),

  fetchContainers: async () => {
    set({ containersLoading: true, error: null });
    try {
      const projectRoot = findProjectRoot();
      const proc = Bun.spawn(
        ["docker", "compose", "ps", "--format", "json", "-a"],
        {
          cwd: projectRoot,
          stdout: "pipe",
          stderr: "pipe",
          env: { ...process.env },
        },
      );

      const [stdout, stderr, exitCode] = await Promise.all([
        new Response(proc.stdout).text(),
        new Response(proc.stderr).text(),
        proc.exited,
      ]);

      if (exitCode !== 0) {
        const errMsg = stderr.trim() || `docker compose ps exited with code ${exitCode}`;
        set({ containers: [], containersLoading: false, error: errMsg });
        return;
      }

      const containers = parseDockerPs(stdout);
      set({ containers, containersLoading: false });
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to query Docker";
      set({ containersLoading: false, error: message });
    }
  },

  fetchConfig: async () => {
    set({ configLoading: true });
    try {
      const projectRoot = findProjectRoot();
      const path = require("node:path");
      const fs = require("node:fs");
      const yamlPath = path.join(projectRoot, "nexus.yaml");
      const file = Bun.file(yamlPath);
      const exists = await file.exists();
      if (exists) {
        const text = await file.text();

        // Resolve all file paths from the config
        let dataDir = path.join(projectRoot, "nexus-data");
        let composeFile = path.join(projectRoot, "nexus-stack.yml");
        const dataDirMatch = text.match(/^data_dir:\s*(.+)$/m);
        if (dataDirMatch?.[1]) {
          const parsed = dataDirMatch[1].trim().replace(/^["']|["']$/g, "");
          dataDir = path.isAbsolute(parsed) ? parsed : path.join(projectRoot, parsed);
        }
        const composeMatch = text.match(/^compose_file:\s*(.+)$/m);
        if (composeMatch?.[1]) {
          const parsed = composeMatch[1].trim().replace(/^["']|["']$/g, "");
          composeFile = path.isAbsolute(parsed) ? parsed : path.join(projectRoot, parsed);
        }

        set({
          configYaml: text,
          configLoading: false,
          paths: {
            projectRoot,
            nexusYaml: yamlPath,
            stateJson: path.join(dataDir, ".state.json"),
            composeFile,
            dataDir,
          },
        });
      } else {
        set({ configYaml: "", configLoading: false, paths: null });
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to read nexus.yaml";
      set({ configYaml: `Error: ${message}`, configLoading: false });
    }
  },

  fetchState: async () => {
    set({ stateLoading: true });
    try {
      const projectRoot = findProjectRoot();
      const path = require("node:path");
      const fs = require("node:fs");

      // Read nexus.yaml to find data_dir
      let dataDir = path.join(projectRoot, "nexus-data");
      try {
        const yaml = fs.readFileSync(path.join(projectRoot, "nexus.yaml"), "utf-8") as string;
        const match = yaml.match(/^data_dir:\s*(.+)$/m);
        if (match?.[1]) {
          const parsed = match[1].trim().replace(/^["']|["']$/g, "");
          dataDir = path.isAbsolute(parsed) ? parsed : path.join(projectRoot, parsed);
        }
      } catch {
        // Use default
      }

      const stateFile = Bun.file(path.join(dataDir, ".state.json"));
      const exists = await stateFile.exists();
      if (exists) {
        const text = await stateFile.text();
        const parsed = JSON.parse(text);
        set({ stateJson: parsed, stateLoading: false });
      } else {
        set({ stateJson: null, stateLoading: false });
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to read .state.json";
      set({ stateJson: null, stateLoading: false, error: message });
    }
  },

  fetchHealth: async (client: FetchClient) => {
    set({ healthLoading: true });
    try {
      const health = await client.get<DetailedHealth>("/health/detailed");
      set({ healthDetails: health, healthLoading: false });
    } catch {
      // Fall back to basic health
      try {
        const basic = await client.get<{ status: string; service: string }>("/health");
        set({
          healthDetails: { status: basic.status, service: basic.service, components: {} },
          healthLoading: false,
        });
      } catch (err) {
        const message = err instanceof Error ? err.message : "Health check failed";
        set({ healthDetails: null, healthLoading: false });
        useErrorStore.getState().pushError({ message, category: categorizeError(message), source: "stack" });
      }
    }
  },

  refreshAll: async (client) => {
    const { fetchContainers, fetchConfig, fetchState, fetchHealth } = get();
    const promises: Promise<void>[] = [
      fetchContainers(),
      fetchConfig(),
      fetchState(),
    ];
    if (client) {
      promises.push(fetchHealth(client));
    }
    await Promise.allSettled(promises);
    set({ lastRefreshed: Date.now() });
  },
}));
