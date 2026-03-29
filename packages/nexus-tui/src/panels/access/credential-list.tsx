/**
 * Credential list with active status badge, DIDs, and delegation depth.
 */

import React from "react";
import type { Credential } from "../../stores/access-store.js";
import { EmptyState } from "../../shared/components/empty-state.js";

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

export function CredentialList({
  credentials,
  loading,
}: CredentialListProps): React.ReactNode {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading credentials...</text>
      </box>
    );
  }

  if (credentials.length === 0) {
    return <EmptyState message="No credentials found." hint="Press i to issue a credential." />;
  }

  return (
    <scrollbox height="100%" width="100%">
      {/* Header */}
      <box height={1} width="100%">
        <text>{"  ST  CREDENTIAL ID     ISSUER DID       SUBJECT DID      DEPTH  EXPIRES            REVOKED"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  --  ---------------  ---------------  ---------------  -----  -----------------  -----------------"}</text>
      </box>

      {/* Rows */}
      {credentials.map((cred) => {
        const badge = cred.is_active ? "●" : "○";

        return (
          <box key={cred.credential_id} height={1} width="100%">
            <text>
              {`  ${badge}   ${shortId(cred.credential_id).padEnd(15)}  ${shortId(cred.issuer_did).padEnd(15)}  ${shortId(cred.subject_did).padEnd(15)}  ${String(cred.delegation_depth).padEnd(5)}  ${formatTimestamp(cred.expires_at).padEnd(17)}  ${formatTimestamp(cred.revoked_at)}`}
            </text>
          </box>
        );
      })}
    </scrollbox>
  );
}
