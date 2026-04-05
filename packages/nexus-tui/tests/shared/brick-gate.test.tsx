/**
 * Render tests for BrickGate component.
 *
 * Covers:
 * - Loading state: spinner shown while featuresLoaded=false
 * - Available: children rendered when brick is enabled
 * - Unavailable: BrickUnavailableMessage shown with correct brick name
 * - Custom fallback: overrides BrickUnavailableMessage
 * - Multi-brick OR: children shown when any brick matches; hidden when none match
 */

import { describe, it, expect, beforeEach, afterEach } from "bun:test";
import React from "react";
import { testRender } from "@opentui/react/test-utils";
import { BrickGate } from "../../src/shared/components/brick-gate.js";
import { useGlobalStore } from "../../src/stores/global-store.js";

// =============================================================================
// Helpers
// =============================================================================

type TestSetup = Awaited<ReturnType<typeof testRender>>;

let setup: TestSetup;

async function renderGate(
  brick: string | readonly string[],
  children: React.ReactNode,
  fallback?: React.ReactNode,
): Promise<string> {
  setup = await testRender(
    <BrickGate brick={brick} fallback={fallback}>
      {children}
    </BrickGate>,
    { width: 80, height: 10 },
  );
  await setup.renderOnce();
  return setup.captureCharFrame();
}

function setStoreState(opts: {
  featuresLoaded: boolean;
  enabledBricks: readonly string[];
  profile?: string | null;
}): void {
  useGlobalStore.setState({
    featuresLoaded: opts.featuresLoaded,
    enabledBricks: opts.enabledBricks,
    profile: opts.profile ?? null,
  });
}

afterEach(() => {
  setup?.renderer.destroy();
  // Reset to clean state
  useGlobalStore.setState({ featuresLoaded: false, enabledBricks: [], profile: null });
});

// =============================================================================
// Tests
// =============================================================================

describe("BrickGate — loading state", () => {
  beforeEach(() => setStoreState({ featuresLoaded: false, enabledBricks: [] }));

  it("shows spinner while features are loading", async () => {
    const frame = await renderGate("storage", <text>Content</text>);
    expect(frame).toContain("Loading features");
  });

  it("does not render children while loading", async () => {
    const frame = await renderGate("storage", <text>SecretContent</text>);
    expect(frame).not.toContain("SecretContent");
  });
});

describe("BrickGate — available", () => {
  beforeEach(() => setStoreState({ featuresLoaded: true, enabledBricks: ["storage", "agent_runtime"] }));

  it("renders children when brick is enabled", async () => {
    const frame = await renderGate("storage", <text>StorageContent</text>);
    expect(frame).toContain("StorageContent");
  });

  it("does not show unavailable message when brick is enabled", async () => {
    const frame = await renderGate("storage", <text>StorageContent</text>);
    expect(frame).not.toContain("Feature not available");
  });
});

describe("BrickGate — unavailable", () => {
  beforeEach(() => setStoreState({ featuresLoaded: true, enabledBricks: ["storage"] }));

  it("shows 'Feature not available' when brick is disabled", async () => {
    const frame = await renderGate("agent_runtime", <text>AgentContent</text>);
    expect(frame).toContain("Feature not available");
  });

  it("shows the required brick name in the message", async () => {
    const frame = await renderGate("agent_runtime", <text>AgentContent</text>);
    expect(frame).toContain("agent_runtime");
  });

  it("does not render children when brick is disabled", async () => {
    const frame = await renderGate("agent_runtime", <text>AgentContent</text>);
    expect(frame).not.toContain("AgentContent");
  });

  it("shows mount guidance text", async () => {
    const frame = await renderGate("agent_runtime", <text>AgentContent</text>);
    expect(frame).toContain("Zones");
  });
});

describe("BrickGate — custom fallback", () => {
  beforeEach(() => setStoreState({ featuresLoaded: true, enabledBricks: [] }));

  it("renders custom fallback instead of BrickUnavailableMessage", async () => {
    const frame = await renderGate(
      "storage",
      <text>Content</text>,
      <text>CustomFallback</text>,
    );
    expect(frame).toContain("CustomFallback");
    expect(frame).not.toContain("Feature not available");
  });
});

describe("BrickGate — multi-brick OR semantics", () => {
  it("shows children when any brick in the array matches", async () => {
    setStoreState({ featuresLoaded: true, enabledBricks: ["delegation"] });
    const frame = await renderGate(
      ["agent_runtime", "delegation"] as const,
      <text>MultiContent</text>,
    );
    expect(frame).toContain("MultiContent");
  });

  it("shows unavailable when no brick in the array matches", async () => {
    setStoreState({ featuresLoaded: true, enabledBricks: ["storage"] });
    const frame = await renderGate(
      ["agent_runtime", "delegation"] as const,
      <text>MultiContent</text>,
    );
    expect(frame).toContain("Feature not available");
    expect(frame).not.toContain("MultiContent");
  });
});
