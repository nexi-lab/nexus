/**
 * Render tests for the RlmAnswerView component.
 *
 * Covers:
 * - No answer + not loading: prompt to ask a question
 * - No answer + loading: "Connecting to RLM..." message
 * - Streaming answer: markdown rendered with streaming=true
 * - Completed answer: markdown rendered with streaming=false
 * - Budget exceeded: error message shown
 * - contextPaths: displayed when present, omitted when empty
 */

import { describe, it, expect, afterEach } from "bun:test";
import React from "react";
import { testRender } from "@opentui/react/test-utils";
import { RlmAnswerView } from "../../src/panels/search/rlm-answer-view.js";
import type { RlmAnswer } from "../../src/stores/search-store-types.js";

// =============================================================================
// Helpers
// =============================================================================

type TestSetup = Awaited<ReturnType<typeof testRender>>;

let setup: TestSetup;

async function renderView(
  answer: RlmAnswer | null,
  loading: boolean,
  contextPaths: readonly string[] = [],
): Promise<string> {
  setup = await testRender(
    <RlmAnswerView answer={answer} loading={loading} contextPaths={contextPaths} />,
    { width: 80, height: 20 },
  );
  await setup.renderOnce();
  return setup.captureCharFrame();
}

const BASE_ANSWER: RlmAnswer = {
  status: "completed",
  answer: "## Result\n\nHere is the answer.",
  total_tokens: 100,
  total_duration_seconds: 1.5,
  iterations: 2,
  error_message: null,
  steps: [],
  model: "test-model",
};

afterEach(() => {
  setup?.renderer.destroy();
});

// =============================================================================
// Tests
// =============================================================================

describe("RlmAnswerView — no answer, not loading", () => {
  it("shows prompt to ask a question", async () => {
    const frame = await renderView(null, false);
    expect(frame).toContain("Press /");
  });

  it("does not show RLM connecting message", async () => {
    const frame = await renderView(null, false);
    expect(frame).not.toContain("Connecting");
  });
});

describe("RlmAnswerView — no answer, loading", () => {
  it("shows connecting message while no answer yet", async () => {
    const frame = await renderView(null, true);
    expect(frame).toContain("Connecting to RLM");
  });
});

describe("RlmAnswerView — streaming answer", () => {
  it("shows status label 'Streaming...'", async () => {
    const answer: RlmAnswer = { ...BASE_ANSWER, status: "streaming", answer: "Partial answer" };
    const frame = await renderView(answer, false);
    expect(frame).toContain("Streaming");
  });

  it("renders the answer content", async () => {
    const answer: RlmAnswer = { ...BASE_ANSWER, status: "streaming", answer: "Partial answer" };
    const frame = await renderView(answer, false);
    expect(frame).toContain("Partial answer");
  });

  it("shows token and iteration counts", async () => {
    const answer: RlmAnswer = { ...BASE_ANSWER, status: "streaming", answer: "hello" };
    const frame = await renderView(answer, false);
    expect(frame).toContain("Tokens:");
    expect(frame).toContain("Iterations:");
  });
});

describe("RlmAnswerView — completed answer", () => {
  it("shows status label 'Completed'", async () => {
    const frame = await renderView(BASE_ANSWER, false);
    expect(frame).toContain("Completed");
  });

  it("renders the answer content", async () => {
    const frame = await renderView(BASE_ANSWER, false);
    // The markdown content should be rendered (at minimum the plain text parts)
    expect(frame).toContain("Result");
  });

  it("does not show 'Streaming...' for completed answers", async () => {
    const frame = await renderView(BASE_ANSWER, false);
    expect(frame).not.toContain("Streaming...");
  });
});

describe("RlmAnswerView — budget exceeded", () => {
  it("shows budget exceeded status label", async () => {
    const answer: RlmAnswer = {
      ...BASE_ANSWER,
      status: "budget_exceeded",
      answer: null,
      error_message: "Token limit reached",
    };
    const frame = await renderView(answer, false);
    expect(frame).toContain("Budget Exceeded");
  });

  it("shows the error message", async () => {
    const answer: RlmAnswer = {
      ...BASE_ANSWER,
      status: "budget_exceeded",
      answer: null,
      error_message: "Token limit reached",
    };
    const frame = await renderView(answer, false);
    expect(frame).toContain("Token limit reached");
  });
});

describe("RlmAnswerView — contextPaths", () => {
  it("shows context paths when present", async () => {
    const frame = await renderView(null, false, ["docs/readme.md", "src/app.ts"]);
    expect(frame).toContain("docs/readme.md");
  });

  it("does not show Docs line when contextPaths is empty", async () => {
    const frame = await renderView(null, false, []);
    expect(frame).not.toContain("Docs:");
  });
});
