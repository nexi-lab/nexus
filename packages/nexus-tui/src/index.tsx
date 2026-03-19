#!/usr/bin/env bun
/**
 * nexus-tui entry point.
 *
 * Parses CLI args, resolves config, and renders the TUI via OpenTUI.
 *
 * Usage:
 *   bunx nexus-tui
 *   bunx nexus-tui --url http://remote:2026 --api-key nx_live_myagent
 *   bunx nexus-tui --agent-id bot-worker-1 --zone-id org_acme
 */

import { createCliRenderer } from "@opentui/core";
import { createRoot } from "@opentui/react";
import { resolveConfig } from "@nexus/api-client";
import { useGlobalStore } from "./stores/global-store.js";
import { App } from "./app.js";

interface CliArgs {
  url?: string;
  apiKey?: string;
  agentId?: string;
  subject?: string;
  zoneId?: string;
}

// Parse CLI arguments
function parseArgs(): CliArgs {
  const args = process.argv.slice(2);
  const result: CliArgs = {};

  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    const next = args[i + 1];

    if ((arg === "--url" || arg === "-u") && next) {
      result.url = next;
      i++;
    } else if ((arg === "--api-key" || arg === "-k") && next) {
      result.apiKey = next;
      i++;
    } else if ((arg === "--agent-id") && next) {
      result.agentId = next;
      i++;
    } else if ((arg === "--subject") && next) {
      result.subject = next;
      i++;
    } else if ((arg === "--zone-id") && next) {
      result.zoneId = next;
      i++;
    } else if (arg === "--help" || arg === "-h") {
      console.log(`
nexus-tui — Terminal UI for Nexus

Usage:
  nexus-tui [options]

Options:
  --url, -u <url>        Nexus server URL (default: NEXUS_URL or http://localhost:2026)
  --api-key, -k <key>    API key (default: NEXUS_API_KEY env var)
  --agent-id <id>        Agent identity (X-Agent-ID header)
  --subject <subject>    Subject override (X-Nexus-Subject header)
  --zone-id <id>         Zone isolation (X-Nexus-Zone-ID header)
  --help, -h             Show this help message

Environment Variables:
  NEXUS_URL              Server URL
  NEXUS_API_KEY          API key
  NEXUS_AGENT_ID         Agent identity
  NEXUS_SUBJECT          Subject override
  NEXUS_ZONE_ID          Zone isolation

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
  // Disable key transformation — all TUI store types use snake_case matching the API wire format
  const config = resolveConfig({
    baseUrl: cliArgs.url,
    apiKey: cliArgs.apiKey,
    agentId: cliArgs.agentId,
    subject: cliArgs.subject,
    zoneId: cliArgs.zoneId,
    transformKeys: false,
  });

  // Initialize global store — testConnection() is called automatically by initConfig()
  // when a client is available (consolidates health + features + auth check, Decision 5A)
  useGlobalStore.getState().initConfig(config);

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
