import type { JSX } from "solid-js";
/**
 * RLM document Q&A answer view: progressive streaming display.
 *
 * Shows document context paths, iteration steps as they arrive via SSE,
 * then the final answer. Status bar shows model, tokens, duration, iteration count.
 */

import type { RlmAnswer, RlmStep } from "../../stores/search-store.js";

interface RlmAnswerViewProps {
  readonly answer: RlmAnswer | null;
  readonly loading: boolean;
  readonly contextPaths: readonly string[];
}

function formatStep(step: RlmStep): string {
  const code = step.code_executed.length > 80
    ? `${step.code_executed.slice(0, 77)}...`
    : step.code_executed;
  const output = step.output_summary.length > 80
    ? `${step.output_summary.slice(0, 77)}...`
    : step.output_summary;
  return `[${step.step}] ${code}\n    → ${output}  (${step.tokens_used} tok, ${step.duration_seconds.toFixed(1)}s)`;
}

export function RlmAnswerView({ answer, loading, contextPaths }: RlmAnswerViewProps): JSX.Element {
  if (!answer && !loading) {
    return (
      <box height="100%" width="100%" flexDirection="column" justifyContent="center" alignItems="center">
        <text>Press / to ask a question about your documents</text>
        {contextPaths.length > 0 ? (
          <text>{`Docs: ${contextPaths.join(", ")} — a:clear`}</text>
        ) : (
          <text>{"Tip: go to Search tab, select results, press 'a' to add document context"}</text>
        )}
      </box>
    );
  }

  if (!answer) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Connecting to RLM...</text>
      </box>
    );
  }

  const statusLabel =
    answer.status === "streaming" ? "Streaming..."
      : answer.status === "completed" ? "Completed"
        : answer.status === "budget_exceeded" ? "Budget Exceeded"
          : "Error";

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Document context paths */}
      {contextPaths.length > 0 && (
        <box height={1} width="100%">
          <text>{`Docs: ${contextPaths.join(", ")}`}</text>
        </box>
      )}

      {/* Status bar */}
      <box height={1} width="100%">
        <text>
          {`${statusLabel}  ${answer.model ? `Model: ${answer.model}  ` : ""}Iterations: ${answer.iterations}  Tokens: ${answer.total_tokens}${answer.total_duration_seconds > 0 ? `  Time: ${answer.total_duration_seconds.toFixed(1)}s` : ""}`}
        </text>
      </box>

      {/* Error / budget message */}
      {answer.error_message && (
        <box height={1} width="100%">
          <text>
            {answer.status === "budget_exceeded"
              ? `Budget exceeded: ${answer.error_message}`
              : `Error: ${answer.error_message}`}
          </text>
        </box>
      )}

      {/* Main content: answer or streaming steps */}
      <scrollbox flexGrow={1} width="100%">
        {answer.answer ? (
          <text>{answer.answer}</text>
        ) : answer.steps.length > 0 ? (
          <box flexDirection="column">
            {answer.steps.map((step) => (
              <box height={2} width="100%">
                <text>{formatStep(step)}</text>
              </box>
            ))}
            {answer.status === "streaming" && (
              <box height={1} width="100%">
                <text>{"Thinking..."}</text>
              </box>
            )}
          </box>
        ) : (
          <text>{answer.status === "streaming" ? "Starting inference..." : "(no answer)"}</text>
        )}
      </scrollbox>
    </box>
  );
}
