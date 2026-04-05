/**
 * Gate component that shows children only when required brick(s) are enabled.
 *
 * When the brick is disabled, shows a standardized "not available" message
 * with profile info and mount guidance (Decision 7A).
 */

import React from "react";
import { useBricksAvailable } from "../hooks/use-brick-available.js";
import { useGlobalStore } from "../../stores/global-store.js";
import { Spinner } from "./spinner.js";
import { textStyle } from "../text-style.js";

interface BrickGateProps {
  /** Brick name or array of brick names (any-of semantics). */
  readonly brick: string | readonly string[];
  /** Content to render when the brick is available. */
  readonly children: React.ReactNode;
  /** Custom fallback. Defaults to BrickUnavailable message. */
  readonly fallback?: React.ReactNode;
}

function BrickUnavailableMessage({ names }: { names: readonly string[] }): React.ReactNode {
  const profile = useGlobalStore((s) => s.profile);
  const brickList = names.join(", ");

  return (
    <box height="100%" width="100%" justifyContent="center" alignItems="center" flexDirection="column">
      <text>{`Feature not available`}</text>
      <text> </text>
      <text style={textStyle({ dim: true })}>{`Required brick${names.length > 1 ? "s" : ""}: ${brickList}`}</text>
      {profile && <text style={textStyle({ dim: true })}>{`Current profile: ${profile}`}</text>}
      <text> </text>
      <text style={textStyle({ dim: true })}>{`To enable: mount the brick via Zones > Bricks`}</text>
    </box>
  );
}

export function BrickGate({ brick, children, fallback }: BrickGateProps): React.ReactNode {
  const bricks = Array.isArray(brick) ? brick : [brick];

  const { available, loading } = useBricksAvailable(bricks);

  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <Spinner label="Loading features..." />
      </box>
    );
  }

  if (!available) {
    return fallback ?? <BrickUnavailableMessage names={bricks} />;
  }

  return <>{children}</>;
}
