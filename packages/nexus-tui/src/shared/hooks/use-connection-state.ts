/**
 * Connection state detection for the PreConnectionScreen (Decision 12A).
 *
 * Pure function exported for testing — follows the detectFreshServer pattern.
 */

import type { ConnectionStatus } from "../../stores/global-store.js";
import type { NexusClientOptions } from "@nexus-ai-fs/api-client";

/**
 * Describes why the TUI cannot connect, guiding the PreConnectionScreen UI.
 *
 *   "no-config"  — No API key configured (client is null)
 *   "no-server"  — Server unreachable (connection error)
 *   "auth-failed" — Server reachable but authentication failed
 *   "connecting" — Still trying to connect
 *   "ready"      — Connected and authenticated
 */
export type ConnectionState =
  | "no-config"
  | "no-server"
  | "auth-failed"
  | "connecting"
  | "ready";

/**
 * Derive a high-level connection state from store values.
 * Pure function — no side effects, fully testable.
 */
export function detectConnectionState(
  connectionStatus: ConnectionStatus,
  connectionError: string | null,
  config: NexusClientOptions,
): ConnectionState {
  // No API key → client was never created
  if (!config.apiKey) {
    return "no-config";
  }

  switch (connectionStatus) {
    case "connected":
      return "ready";

    case "connecting":
    case "disconnected":
      return "connecting";

    case "error": {
      if (!connectionError) return "no-server";

      // Distinguish auth failures from network failures
      const lower = connectionError.toLowerCase();
      if (
        lower.includes("unauthorized") ||
        lower.includes("forbidden") ||
        lower.includes("401") ||
        lower.includes("403")
      ) {
        return "auth-failed";
      }
      return "no-server";
    }

    default:
      return "connecting";
  }
}
