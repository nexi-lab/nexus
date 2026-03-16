/**
 * Welcome screen shown on fresh servers.
 * Offers to seed demo data or start with empty server.
 */
import React, { useState } from "react";
import { useKeyboard } from "../hooks/use-keyboard.js";
import { useGlobalStore } from "../../stores/global-store.js";
import { statusColor } from "../theme.js";
import { Spinner } from "./spinner.js";

interface WelcomeScreenProps {
  readonly onDismiss: () => void;
}

export function WelcomeScreen({ onDismiss }: WelcomeScreenProps): React.ReactNode {
  const client = useGlobalStore((s) => s.client);
  const config = useGlobalStore((s) => s.config);
  const serverVersion = useGlobalStore((s) => s.serverVersion);
  const [seeding, setSeeding] = useState(false);
  const [showDetails, setShowDetails] = useState(false);

  useKeyboard(seeding ? {} : {
    "y": async () => {
      if (!client) return;
      setSeeding(true);
      try {
        await client.post("/api/v2/admin/demo/seed", {});
        onDismiss();
      } catch {
        setSeeding(false);
      }
    },
    "n": onDismiss,
    "?": () => setShowDetails((prev) => !prev),
    "escape": onDismiss,
  });

  const baseUrl = config.baseUrl ?? "localhost:2026";

  return (
    <box height="100%" width="100%" justifyContent="center" alignItems="center">
      <box
        flexDirection="column"
        borderStyle="double"
        width={56}
        padding={1}
      >
        <text bold foregroundColor={statusColor.info}>
          {"    \u2554\u2557\u2554\u250C\u2500\u2510\u2500\u2510 \u2510\u252C\u2510 \u252C\u250C\u2500\u2510"}
        </text>
        <text bold foregroundColor={statusColor.info}>
          {"    \u2551\u2551\u2551\u251C\u2524 \u250C\u2524 \u2502 \u2502\u2502\u2514\u2500\u2510"}
        </text>
        <text bold foregroundColor={statusColor.info}>
          {"    \u255D\u255A\u255D\u2514\u2500\u2518\u2518\u2514 \u2514\u2500\u2518\u2514\u2500\u2518"}
        </text>
        <text>{""}</text>
        <text dimColor>{`  Connected to ${baseUrl}${serverVersion ? ` (v${serverVersion})` : ""}`}</text>
        <text>{""}</text>
        <text>{"  This server has no data yet. Would you like"}</text>
        <text>{"  to seed demo content?"}</text>
        <text>{""}</text>
        {seeding ? (
          <Spinner label="  Seeding demo data..." />
        ) : (
          <>
            <text>
              <text foregroundColor={statusColor.info}>{"  [Y]"}</text>
              <text>{" Seed demo data (files, agents, permissions)"}</text>
            </text>
            <text>
              <text foregroundColor={statusColor.dim}>{"  [N]"}</text>
              <text>{" Start with empty server"}</text>
            </text>
            <text>
              <text foregroundColor={statusColor.dim}>{"  [?]"}</text>
              <text>{" What's in the demo data?"}</text>
            </text>
          </>
        )}
        {showDetails && (
          <>
            <text>{""}</text>
            <text dimColor>{"  Demo data includes:"}</text>
            <text dimColor>{"  \u2022 12 sample files (markdown, code, data)"}</text>
            <text dimColor>{"  \u2022 2 user identities"}</text>
            <text dimColor>{"  \u2022 1 agent with trajectories"}</text>
            <text dimColor>{"  \u2022 HERB evaluation corpus"}</text>
            <text dimColor>{"  \u2022 ReBAC permission policies"}</text>
          </>
        )}
      </box>
    </box>
  );
}
