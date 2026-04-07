import type { JSX } from "solid-js";
/**
 * Credential list with active status badge, DIDs, and delegation depth.
 */

import type { Credential } from "../../stores/access-store.js";

interface CredentialListProps {
  readonly credentials: readonly Credential[];
  readonly loading: boolean;
}

function shortId(id: string): string {
  if (id.length <= 16) return id;
  return `${id.slice(0, 12)}..`;
}

function formatTimestamp(ts: string | null): string {
  if (!ts) return "never";
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

export function CredentialList(props: CredentialListProps): JSX.Element {
  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>
        {props.loading
          ? "Loading credentials..."
          : props.credentials.length === 0
            ? "No credentials found. Press i to issue a credential."
            : `${props.credentials.length} credentials`}
      </text>
      <scrollbox flexGrow={1} width="100%">
        {/* Header */}
        <box height={1} width="100%">
          <text>{"  ST  CREDENTIAL ID     ISSUER DID       SUBJECT DID      DEPTH  EXPIRES            REVOKED"}</text>
        </box>
        <box height={1} width="100%">
          <text>{"  --  ---------------  ---------------  ---------------  -----  -----------------  -----------------"}</text>
        </box>

        {/* Rows */}
        {props.credentials.map((cred) => {
          const badge = cred.is_active ? "●" : "○";

          return (
            <box height={1} width="100%">
              <text>
                {`  ${badge}   ${shortId(cred.credential_id).padEnd(15)}  ${shortId(cred.issuer_did).padEnd(15)}  ${shortId(cred.subject_did).padEnd(15)}  ${String(cred.delegation_depth).padEnd(5)}  ${formatTimestamp(cred.expires_at).padEnd(17)}  ${formatTimestamp(cred.revoked_at)}`}
              </text>
            </box>
          );
        })}
      </scrollbox>
    </box>
  );
}
