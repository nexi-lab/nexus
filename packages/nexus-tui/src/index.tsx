#!/usr/bin/env bun
/**
 * nexus-tui entry point.
 *
 * Parses CLI args, resolves config, and renders the TUI via OpenTUI.
 *
 * Usage:
 *   bunx nexus-tui
 *   bunx nexus-tui --url http://remote:2026 --api-key nx_live_myagent
 */

import { createCliRenderer } from "@opentui/core";
import { createRoot } from "@opentui/react";
import { resolveConfig } from "@nexus/api-client";
import { useGlobalStore } from "./stores/global-store.js";
import { App } from "./app.js";

// Parse CLI arguments
function parseArgs(): { url?: string; apiKey?: string } {
  const args = process.argv.slice(2);
  const result: { url?: string; apiKey?: string } = {};

  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    const next = args[i + 1];

    if ((arg === "--url" || arg === "-u") && next) {
      result.url = next;
      i++;
    } else if ((arg === "--api-key" || arg === "-k") && next) {
      result.apiKey = next;
      i++;
    } else if (arg === "--help" || arg === "-h") {
      console.log(`
nexus-tui — Terminal UI for Nexus

Usage:
  nexus-tui [options]

Options:
  --url, -u <url>        Nexus server URL (default: NEXUS_URL or http://localhost:2026)
  --api-key, -k <key>    API key (default: NEXUS_API_KEY env var)
  --help, -h             Show this help message

Environment Variables:
  NEXUS_URL              Server URL
  NEXUS_API_KEY          API key

Config File:
  ~/.nexus/config.yaml   Auto-discovered (same as nexus CLI)
`.trim());
      process.exit(0);
    }
  }

  return result;
}

async function main(): Promise<void> {
  const cliArgs = parseArgs();

  // Resolve config: CLI args > env vars > config file > defaults
  const config = resolveConfig({
    baseUrl: cliArgs.url,
    apiKey: cliArgs.apiKey,
  });

  // Initialize global store
  useGlobalStore.getState().initConfig({
    baseUrl: config.baseUrl,
    apiKey: config.apiKey,
  });

  // Test connection in background (non-blocking — TUI renders immediately)
  const client = useGlobalStore.getState().client;
  if (client) {
    useGlobalStore.getState().setConnectionStatus("connecting");
    client
      .get<{ version?: string; zone_id?: string; uptime_seconds?: number }>(
        "/api/v2/bricks/health",
      )
      .then((info) => {
        useGlobalStore.getState().setConnectionStatus("connected");
        useGlobalStore.getState().setServerInfo({
          version: info.version,
          zoneId: info.zone_id,
          uptime: info.uptime_seconds,
        });
      })
      .catch(() => {
        useGlobalStore.getState().setConnectionStatus("error", "Failed to connect to server");
      });
  }

  // Create OpenTUI renderer and mount the React tree
  const renderer = await createCliRenderer({
    exitOnCtrlC: true,
    useAlternateScreen: true,
  });

  createRoot(renderer).render(<App />);
}

main().catch((err) => {
  console.error("Fatal error:", err);
  process.exit(1);
});
