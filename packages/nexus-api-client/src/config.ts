/**
 * Config resolution from multiple sources with precedence:
 *
 * 1. Explicit overrides (constructor args / CLI flags)
 * 2. Config file (./nexus.yaml → ./nexus.yml → ~/.nexus/config.yaml)
 * 3. Environment variables (NEXUS_URL, NEXUS_API_KEY, etc.)
 * 4. Defaults
 *
 * Matches the Python CLI (`config.py:_auto_discover`): if a config file
 * exists, its values take priority over environment variables.
 */

import type { NexusClientOptions } from "./types.js";

const DEFAULT_BASE_URL = "http://localhost:2026";

/**
 * Resolve Nexus client config from multiple sources.
 *
 * Works in Node, Bun, and Deno. Gracefully degrades in browsers
 * (no env vars or filesystem — uses defaults + overrides only).
 */
export function resolveConfig(
  overrides?: Partial<NexusClientOptions>,
): NexusClientOptions {
  // Layer 2: Config file — check ./nexus.yaml, ./nexus.yml, then ~/.nexus/config.yaml
  const yamlConfig = readYamlConfig();

  // Layer 3: Environment variables (lower priority than config file)
  const envUrl = readEnv("NEXUS_URL");
  const envApiKey = readEnv("NEXUS_API_KEY");

  // Layer 1 wins over 2 (file) wins over 3 (env) wins over defaults
  return {
    apiKey: overrides?.apiKey ?? yamlConfig.apiKey ?? envApiKey ?? "",
    baseUrl: overrides?.baseUrl ?? yamlConfig.url ?? envUrl ?? DEFAULT_BASE_URL,
    timeout: overrides?.timeout,
    maxRetries: overrides?.maxRetries,
    fetch: overrides?.fetch,
    transformKeys: overrides?.transformKeys,
    agentId: overrides?.agentId ?? yamlConfig.agentId ?? readEnv("NEXUS_AGENT_ID"),
    subject: overrides?.subject ?? readEnv("NEXUS_SUBJECT"),
    zoneId: overrides?.zoneId ?? yamlConfig.zoneId ?? readEnv("NEXUS_ZONE_ID"),
  };
}

// =============================================================================
// Internal helpers
// =============================================================================

function readEnv(name: string): string | undefined {
  try {
    // Works in Node, Bun, Deno
    return typeof process !== "undefined" ? process.env[name] : undefined;
  } catch {
    return undefined;
  }
}

interface YamlConfig {
  url?: string;
  apiKey?: string;
  agentId?: string;
  zoneId?: string;
}

/**
 * Minimal YAML reader matching the Python CLI search order:
 *   1. ./nexus.yaml
 *   2. ./nexus.yml
 *   3. ~/.nexus/config.yaml
 *
 * Only reads top-level `url:` and `api_key:` fields via regex.
 * Avoids pulling in a full YAML parser dependency.
 */
function readYamlConfig(): YamlConfig {
  try {
    // Dynamic import to avoid bundler issues in browsers
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const fs = require("node:fs") as typeof import("node:fs");
    const os = require("node:os") as typeof import("node:os");
    const path = require("node:path") as typeof import("node:path");

    // Search order: CWD first, then home dir.
    // Each TUI instance uses its own nexus.yaml in its CWD to avoid
    // conflicts with other running Nexus stacks.
    const candidates = [
      path.resolve("nexus.yaml"),
      path.resolve("nexus.yml"),
      path.join(os.homedir(), ".nexus", "config.yaml"),
    ];

    for (const configPath of candidates) {
      try {
        const content = fs.readFileSync(configPath, "utf-8");
        let url = extractYamlValue(content, "url");
        const apiKey = extractYamlValue(content, "api_key");
        const agentId = extractYamlValue(content, "agent_id");
        const zoneId = extractYamlValue(content, "zone_id");

        // If no explicit url but ports.http exists, build URL from port
        // (nexus init --preset shared/demo writes ports.http instead of url)
        if (!url) {
          const httpPort = extractIndentedYamlValue(content, "ports", "http");
          if (httpPort) {
            url = `http://localhost:${httpPort}`;
          }
        }

        return {
          url: url ?? undefined,
          apiKey: apiKey ?? undefined,
          agentId: agentId ?? undefined,
          zoneId: zoneId ?? undefined,
        };
      } catch {
        // File doesn't exist or isn't readable, try next candidate
      }
    }

    return {};
  } catch {
    return {};
  }
}

function extractYamlValue(content: string, key: string): string | null {
  const regex = new RegExp(`^${key}:\\s*["']?([^"'\\n]+)["']?`, "m");
  const match = regex.exec(content);
  return match?.[1]?.trim() ?? null;
}

/**
 * Extract a nested YAML value like `ports:\n  http: 2027`.
 * Only handles one level of nesting with 2-space indent.
 */
function extractIndentedYamlValue(content: string, parent: string, child: string): string | null {
  const regex = new RegExp(`^${parent}:\\s*\\n(?:[ ]{2}\\w+:[^\\n]*\\n)*?[ ]{2}${child}:\\s*["']?([^"'\\n]+)["']?`, "m");
  const match = regex.exec(content);
  return match?.[1]?.trim() ?? null;
}
