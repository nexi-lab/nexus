import { FetchClient } from "./fetch-client.js";
import type { NexusClientOptions, RequestOptions } from "./types.js";

export interface ZoneRef {
  zoneId: string;
  displayName: string;
  status: "active" | "suspended" | "deleting" | "deleted" | string;
  revision: string;
  [key: string]: unknown;
}

export interface ZoneOperation {
  operationId: string;
  action: string;
  state: "queued" | "running" | "waiting_dependency" | "succeeded" | "failed";
  step: string;
  retryable: boolean;
  error?: { code: string; message: string; retryable: boolean };
  [key: string]: unknown;
}

export interface ZoneCreateInput {
  zoneId: string;
  displayName: string;
  description?: string;
  deployment?: Record<string, unknown>;
  labels?: Record<string, string>;
}

export interface ZoneGrantInput {
  grantee: { subjectType: string; subjectId: string; trustDomain?: string };
  capabilities: string[];
  resourcePrefixes?: string[];
  source?: { sourceType: string; sourceId: string };
  reason: string;
  policyVersion?: string;
  notBefore?: string;
  expiresAt?: string;
}

export interface ZoneMount {
  mountId: string;
  parentZoneId: string;
  targetZoneId: string;
  path: string;
  desiredState: string;
  observedState?: string;
  runtimeRevision?: string;
}

export interface ZoneDelegationInput {
  userId: string;
  orgId: string;
  membershipVersion: string;
  zoneId: string;
  audience: string;
  ttlS?: number;
  grantId?: string;
  purpose?: "data-access" | "runtime";
  scopeRules?: ZoneDelegationScopeRule[];
}

export interface ZoneDelegationScopeRule {
  capability: string;
  resourcePrefixes: string[];
}

export interface ZoneDelegation {
  delegationId: string;
  userId: string;
  orgId: string;
  zoneId: string;
  grantId: string;
  grantRevision: string;
  authorizationEpoch: number;
  audience: string;
  purpose?: "data-access" | "runtime";
  scopeRules?: ZoneDelegationScopeRule[];
  expiresAt: string;
  status: string;
}

/** Typed client for the canonical Zone v1 HTTP boundary. */
export class ZoneClient {
  private readonly http: FetchClient;

  constructor(options: NexusClientOptions | FetchClient) {
    this.http = options instanceof FetchClient ? options : new FetchClient(options);
  }

  capabilities(): Promise<Record<string, unknown>> {
    return this.http.get("/v2/zone-capabilities");
  }

  list(query = ""): Promise<{ zones: ZoneRef[]; nextCursor?: string }> {
    return this.http.get(`/v2/zones${query ? `?${query}` : ""}`);
  }

  get(zoneId: string): Promise<ZoneRef> {
    return this.http.get(`/v2/zones/${encodeURIComponent(zoneId)}`);
  }

  create(body: ZoneCreateInput, idempotencyKey: string): Promise<ZoneOperation> {
    return this.http.post("/v2/zones", body, { idempotencyKey });
  }

  patch(
    zoneId: string,
    body: Partial<ZoneCreateInput>,
    revision: string,
    idempotencyKey: string,
  ): Promise<ZoneRef> {
    return this.http.patch(`/v2/zones/${encodeURIComponent(zoneId)}`, body, {
      idempotencyKey,
      headers: { "If-Match": revision },
    });
  }

  suspend(zoneId: string, idempotencyKey: string): Promise<ZoneOperation> {
    return this.http.post(`/v2/zones/${encodeURIComponent(zoneId)}:suspend`, {}, {
      idempotencyKey,
    });
  }

  resume(zoneId: string, idempotencyKey: string): Promise<ZoneOperation> {
    return this.http.post(`/v2/zones/${encodeURIComponent(zoneId)}:resume`, {}, {
      idempotencyKey,
    });
  }

  deprovision(zoneId: string, idempotencyKey: string): Promise<ZoneOperation> {
    return this.http.delete(`/v2/zones/${encodeURIComponent(zoneId)}`, {
      idempotencyKey,
      headers: { "X-Nexus-Confirm-Zone": zoneId },
    });
  }

  join(
    zoneId: string,
    peers: string[],
    idempotencyKey: string,
    learner = false,
  ): Promise<ZoneOperation> {
    return this.http.post(
      `/v2/zones/${encodeURIComponent(zoneId)}/joins`,
      { peers, learner },
      { idempotencyKey },
    );
  }

  createGrant(
    zoneId: string,
    body: ZoneGrantInput,
    idempotencyKey: string,
  ): Promise<ZoneOperation> {
    return this.http.post(`/v2/zones/${encodeURIComponent(zoneId)}/grants`, body, {
      idempotencyKey,
    });
  }

  listGrants(zoneId: string, query = ""): Promise<{ grants: unknown[]; nextCursor?: string }> {
    return this.http.get(
      `/v2/zones/${encodeURIComponent(zoneId)}/grants${query ? `?${query}` : ""}`,
    );
  }

  getGrant(zoneId: string, grantId: string): Promise<unknown> {
    return this.http.get(
      `/v2/zones/${encodeURIComponent(zoneId)}/grants/${encodeURIComponent(grantId)}`,
    );
  }

  revokeGrant(zoneId: string, grantId: string, idempotencyKey: string): Promise<unknown> {
    return this.http.delete(
      `/v2/zones/${encodeURIComponent(zoneId)}/grants/${encodeURIComponent(grantId)}`,
      { idempotencyKey },
    );
  }

  operation(operationId: string): Promise<ZoneOperation> {
    return this.http.get(`/v2/zone-operations/${encodeURIComponent(operationId)}`);
  }

  issueDelegation(body: ZoneDelegationInput, idempotencyKey: string): Promise<ZoneDelegation> {
    return this.http.post("/v2/auth/zone-delegations", body, { idempotencyKey });
  }

  getDelegation(delegationId: string): Promise<ZoneDelegation> {
    return this.http.get(`/v2/auth/zone-delegations/${encodeURIComponent(delegationId)}`);
  }

  revokeDelegation(delegationId: string, idempotencyKey: string): Promise<unknown> {
    return this.http.delete(`/v2/auth/zone-delegations/${encodeURIComponent(delegationId)}`, {
      idempotencyKey,
    });
  }

  createMount(
    body: { parentZoneId: string; targetZoneId: string; path: string },
    idempotencyKey: string,
  ): Promise<ZoneOperation> {
    return this.http.post("/v2/zone-mounts", body, { idempotencyKey });
  }

  listMounts(zoneId: string): Promise<{ mounts: ZoneMount[]; nextCursor?: string }> {
    return this.http.get(`/v2/zone-mounts?zone_id=${encodeURIComponent(zoneId)}`);
  }

  unmount(mountId: string, idempotencyKey: string): Promise<ZoneOperation> {
    return this.http.delete(`/v2/zone-mounts/${encodeURIComponent(mountId)}`, {
      idempotencyKey,
    });
  }

  transfer(
    source: Record<string, unknown>,
    target: Record<string, unknown>,
    idempotencyKey: string,
  ): Promise<ZoneOperation> {
    return this.http.post("/v2/zone-transfers", { source, target }, { idempotencyKey });
  }

  transferOperation(operationId: string): Promise<ZoneOperation> {
    return this.http.get(`/v2/zone-transfers/${encodeURIComponent(operationId)}`);
  }
}
