/**
 * Credential list with status badge, type, and issuer.
 */

import React from "react";
import type { Credential } from "../../stores/access-store.js";

interface CredentialListProps {
  readonly credentials: readonly Credential[];
  readonly loading: boolean;
}

const STATUS_BADGES: Readonly<Record<Credential["status"], string>> = {
  active: "●",
  revoked: "✗",
  expired: "○",
};

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
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>No credentials found</text>
      </box>
    );
  }

  return (
    <scrollbox height="100%" width="100%">
      {/* Header */}
      <box height={1} width="100%">
        <text>{"  ST  TYPE             ISSUER           SUBJECT          ISSUED             EXPIRES            STATUS"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  --  ---------------  ---------------  ---------------  -----------------  -----------------  -------"}</text>
      </box>

      {/* Rows */}
      {credentials.map((cred) => {
        const badge = STATUS_BADGES[cred.status] ?? "?";

        return (
          <box key={cred.credential_id} height={1} width="100%">
            <text>
              {`  ${badge}   ${cred.type.padEnd(15)}  ${shortId(cred.issuer).padEnd(15)}  ${shortId(cred.subject).padEnd(15)}  ${formatTimestamp(cred.issued_at).padEnd(17)}  ${formatTimestamp(cred.expires_at).padEnd(17)}  ${cred.status}`}
            </text>
          </box>
        );
      })}
    </scrollbox>
  );
}
