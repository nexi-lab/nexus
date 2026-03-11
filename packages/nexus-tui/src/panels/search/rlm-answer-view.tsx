/**
 * RLM Q&A answer view: displays the result from POST /api/v2/rlm/infer.
 */

import React from "react";
import type { RlmAnswer } from "../../stores/search-store.js";

interface RlmAnswerViewProps {
  readonly answer: RlmAnswer | null;
  readonly loading: boolean;
}

export function RlmAnswerView({ answer, loading }: RlmAnswerViewProps): React.ReactNode {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Thinking...</text>
      </box>
    );
  }

  if (!answer) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Press / to ask a question, then Enter to submit</text>
      </box>
    );
  }

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Status line */}
      <box height={1} width="100%">
        <text>
          {`Status: ${answer.status}  Iterations: ${answer.iterations}  Tokens: ${answer.total_tokens}  Time: ${answer.total_duration_seconds.toFixed(1)}s`}
        </text>
      </box>

      {/* Error message if any */}
      {answer.error_message && (
        <box height={1} width="100%">
          <text>{`Error: ${answer.error_message}`}</text>
        </box>
      )}

      {/* Answer content */}
      <scrollbox flexGrow={1} width="100%">
        <text>{answer.answer ?? "(no answer)"}</text>
      </scrollbox>
    </box>
  );
}
