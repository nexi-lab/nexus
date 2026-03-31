/**
 * Welcome screen shown on fresh servers.
 * Offers to seed demo data or start with empty server.
 */
import React, { useState } from "react";
import { useKeyboard } from "../hooks/use-keyboard.js";
import { useGlobalStore } from "../../stores/global-store.js";
import { statusColor } from "../theme.js";
import { Spinner } from "./spinner.js";
import { textStyle } from "../text-style.js";

interface WelcomeScreenProps {
  readonly onDismiss: () => void;
}

export function WelcomeScreen({ onDismiss }: WelcomeScreenProps): React.ReactNode {
  const client = useGlobalStore((s) => s.client);
  const config = useGlobalStore((s) => s.config);
  const serverVersion = useGlobalStore((s) => s.serverVersion);
  const [seeding, setSeeding] = useState(false);
  const [seedError, setSeedError] = useState<string | null>(null);
  const [showDetails, setShowDetails] = useState(false);

  useKeyboard(seeding ? {} : {
    "y": async () => {
      if (!client) return;
      setSeeding(true);
      setSeedError(null);
      try {
        await client.post("/api/v2/admin/demo/seed", {});
        onDismiss();
      } catch {
        setSeeding(false);
        setSeedError("Seeding failed. Press Y to retry or N to skip.");
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
        <text style={textStyle({ fg: "#00d4ff", bold: true })}>
          {"    _   _ _____ __  __ _   _ ____"}
        </text>
        <text style={textStyle({ fg: "#00b8ff", bold: true })}>
          {"   | \\ | | ____|  \\/  | | | / ___|"}
        </text>
        <text style={textStyle({ fg: "#4d8eff", bold: true })}>
          {"   |  \\| |  _|  >\\/< | | | \\___ \\"}
        </text>
        <text style={textStyle({ fg: "#8066ff", bold: true })}>
          {"   | |\\  | |___/ /\\ \\| |_| |___) |"}
        </text>
        <text style={textStyle({ fg: "#b44dff", bold: true })}>
          {"   |_| \\_|_____/_/  \\_\\\\___/|____/"}
        </text>
        <text>{""}</text>
        <text style={textStyle({ dim: true })}>{`  Connected to ${baseUrl}${serverVersion ? ` (v${serverVersion})` : ""}`}</text>
        <text>{""}</text>
        <text>{"  This server has no data yet. Would you like"}</text>
        <text>{"  to seed demo content?"}</text>
        <text>{""}</text>
        {seeding ? (
          <Spinner label="  Seeding demo data..." />
        ) : (
          <>
            <text>
              <span style={textStyle({ fg: statusColor.info })}>{"  [Y]"}</span>
              <span>{" Seed demo data (files, agents, permissions)"}</span>
            </text>
            <text>
              <span style={textStyle({ fg: statusColor.dim })}>{"  [N]"}</span>
              <span>{" Start with empty server"}</span>
            </text>
            <text>
              <span style={textStyle({ fg: statusColor.dim })}>{"  [?]"}</span>
              <span>{" What's in the demo data?"}</span>
            </text>
          </>
        )}
        {seedError && (
          <>
            <text>{""}</text>
            <text style={textStyle({ fg: statusColor.error })}>{"  "}{seedError}</text>
          </>
        )}
        {showDetails && (
          <>
            <text>{""}</text>
            <text style={textStyle({ dim: true })}>{"  Demo data includes:"}</text>
            <text style={textStyle({ dim: true })}>{"  \u2022 12 sample files (markdown, code, data)"}</text>
            <text style={textStyle({ dim: true })}>{"  \u2022 2 user identities"}</text>
            <text style={textStyle({ dim: true })}>{"  \u2022 1 agent with trajectories"}</text>
            <text style={textStyle({ dim: true })}>{"  \u2022 HERB evaluation corpus"}</text>
            <text style={textStyle({ dim: true })}>{"  \u2022 ReBAC permission policies"}</text>
          </>
        )}
      </box>
    </box>
  );
}
