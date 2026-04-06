import { Show } from "solid-js";
import type { JSX } from "solid-js";
/**
 * Connector capabilities detail view.
 *
 * Shown when Enter is pressed on a selected connector.
 * Fetches and displays capabilities from GET /api/v2/connectors/{name}/capabilities.
 */


import { Spinner } from "../../shared/components/spinner.js";

export interface ConnectorDetailProps {
  readonly connectorName: string;
  readonly capabilities: unknown | null;
  readonly loading: boolean;
}

export function ConnectorDetail(props: ConnectorDetailProps): JSX.Element {
  const displayLines = () => {
    if (props.capabilities === null || props.capabilities === undefined) return null;
    const json = JSON.stringify(props.capabilities, null, 2);
    const display = json.length > 5000 ? json.slice(0, 5000) + "\n... (truncated)" : json;
    return display.split("\n");
  };

  return (
    <Show
      when={!props.loading}
      fallback={<Spinner label={`Loading capabilities for ${props.connectorName}...`} />}
    >
      <Show
        when={displayLines()}
        fallback={<text>{`No capabilities data for ${props.connectorName}`}</text>}
      >
        <box flexDirection="column" height="100%" width="100%">
          <box height={1} width="100%">
            <text>{`Capabilities: ${props.connectorName}`}</text>
          </box>
          <scrollbox flexGrow={1} width="100%">
            {displayLines()!.map((line, _i) => (
              <box height={1} width="100%">
                <text>{`  ${line}`}</text>
              </box>
            ))}
          </scrollbox>
        </box>
      </Show>
    </Show>
  );
}
