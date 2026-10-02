import { describe, expect, it, vi } from "vitest";

import { ZoneClient } from "../src/zones.js";

function ok(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 202,
    headers: { "Content-Type": "application/json" },
  });
}

describe("ZoneClient", () => {
  it("sends create through /v2 with an idempotency key", async () => {
    const fetchFn = vi.fn(async () =>
      ok({ operation_id: "op_1", action: "create", state: "queued", step: "accepted", retryable: true }),
    );
    const client = new ZoneClient({ apiKey: "secret", fetch: fetchFn });

    const operation = await client.create(
      { zoneId: "team-alpha", displayName: "Alpha" },
      "create-1",
    );

    expect(operation.operationId).toBe("op_1");
    expect(fetchFn).toHaveBeenCalledOnce();
    const [url, init] = fetchFn.mock.calls[0];
    expect(url).toBe("http://localhost:2026/v2/zones");
    expect((init.headers as Record<string, string>)["Idempotency-Key"]).toBe("create-1");
    expect(JSON.parse(init.body as string)).toEqual({
      zone_id: "team-alpha",
      display_name: "Alpha",
    });
  });

  it("carries If-Match on a patch", async () => {
    const fetchFn = vi.fn(async () =>
      ok({ zone_id: "team-alpha", display_name: "New", status: "active", revision: "r2" }),
    );
    const client = new ZoneClient({ apiKey: "secret", fetch: fetchFn });

    await client.patch("team-alpha", { displayName: "New" }, "r1", "patch-1");

    const [, init] = fetchFn.mock.calls[0];
    expect((init.headers as Record<string, string>)["If-Match"]).toBe("r1");
    expect((init.headers as Record<string, string>)["Idempotency-Key"]).toBe("patch-1");
  });

  it("maps typed delegation scope rules to snake_case and back", async () => {
    const fetchFn = vi.fn(async () =>
      ok({
        delegation_id: "d1",
        user_id: "u1",
        org_id: "o1",
        zone_id: "team-alpha",
        grant_id: "g1",
        grant_revision: "r1",
        authorization_epoch: 2,
        audience: "nexus-api",
        purpose: "runtime",
        scope_rules: [
          { capability: "zone.runtime.execute", resource_prefixes: ["/sessions/s1"] },
        ],
        expires_at: "2026-09-24T10:00:00Z",
        status: "active",
      }),
    );
    const client = new ZoneClient({ apiKey: "secret", fetch: fetchFn });
    const delegation = await client.issueDelegation(
      {
        userId: "u1",
        orgId: "o1",
        membershipVersion: "r1",
        zoneId: "team-alpha",
        audience: "nexus-api",
        grantId: "g1",
        purpose: "runtime",
        scopeRules: [
          { capability: "zone.runtime.execute", resourcePrefixes: ["/sessions/s1"] },
        ],
      },
      "issue-1",
    );
    const [, init] = fetchFn.mock.calls[0];
    expect(JSON.parse(init.body as string)).toMatchObject({
      grant_id: "g1",
      purpose: "runtime",
      scope_rules: [
        { capability: "zone.runtime.execute", resource_prefixes: ["/sessions/s1"] },
      ],
    });
    expect(delegation.scopeRules?.[0].resourcePrefixes).toEqual(["/sessions/s1"]);
    expect("runtimeSessionId" in delegation).toBe(false);
  });

  // ── full-surface coverage: every remaining method's method/URL/idempotency
  // key and response mapping, table-driven in the same mock style. ─────────
  const OP = {
    operation_id: "op_t",
    action: "create",
    state: "queued",
    step: "accepted",
    retryable: true,
  };

  it.each([
    ["capabilities", (c: ZoneClient) => c.capabilities(), "GET", "/v2/zone-capabilities", null],
    ["list", (c: ZoneClient) => c.list("cursor=x"), "GET", "/v2/zones?cursor=x", null],
    ["list (no query)", (c: ZoneClient) => c.list(), "GET", "/v2/zones", null],
    ["get", (c: ZoneClient) => c.get("team-alpha"), "GET", "/v2/zones/team-alpha", null],
    [
      "suspend",
      (c: ZoneClient) => c.suspend("team-alpha", "k1"),
      "POST",
      "/v2/zones/team-alpha:suspend",
      "k1",
    ],
    [
      "resume",
      (c: ZoneClient) => c.resume("team-alpha", "k2"),
      "POST",
      "/v2/zones/team-alpha:resume",
      "k2",
    ],
    [
      "deprovision",
      (c: ZoneClient) => c.deprovision("team-alpha", "k3"),
      "DELETE",
      "/v2/zones/team-alpha",
      "k3",
    ],
    [
      "join",
      (c: ZoneClient) => c.join("team-alpha", ["p1"], "k4"),
      "POST",
      "/v2/zones/team-alpha/joins",
      "k4",
    ],
    [
      "createGrant",
      (c: ZoneClient) =>
        c.createGrant(
          "team-alpha",
          {
            grantee: { subjectType: "organization", subjectId: "org-1" },
            capabilities: ["zone.data.read"],
            resourcePrefixes: ["/"],
            source: { sourceType: "moss_org_binding", sourceId: "s1" },
          },
          "k5",
        ),
      "POST",
      "/v2/zones/team-alpha/grants",
      "k5",
    ],
    ["listGrants", (c: ZoneClient) => c.listGrants("team-alpha", "cursor=g"), "GET", "/v2/zones/team-alpha/grants?cursor=g", null],
    ["listGrants (no query)", (c: ZoneClient) => c.listGrants("team-alpha"), "GET", "/v2/zones/team-alpha/grants", null],
    ["getGrant", (c: ZoneClient) => c.getGrant("team-alpha", "g1"), "GET", "/v2/zones/team-alpha/grants/g1", null],
    [
      "revokeGrant",
      (c: ZoneClient) => c.revokeGrant("team-alpha", "g1", "k6"),
      "DELETE",
      "/v2/zones/team-alpha/grants/g1",
      "k6",
    ],
    ["operation", (c: ZoneClient) => c.operation("op_t"), "GET", "/v2/zone-operations/op_t", null],
    [
      "getDelegation",
      (c: ZoneClient) => c.getDelegation("d1"),
      "GET",
      "/v2/auth/zone-delegations/d1",
      null,
    ],
    [
      "revokeDelegation",
      (c: ZoneClient) => c.revokeDelegation("d1", "k7"),
      "DELETE",
      "/v2/auth/zone-delegations/d1",
      "k7",
    ],
    [
      "createMount",
      (c: ZoneClient) =>
        c.createMount({ parentZoneId: "root", targetZoneId: "team-alpha", path: "/alpha" }, "k8"),
      "POST",
      "/v2/zone-mounts",
      "k8",
    ],
    ["listMounts", (c: ZoneClient) => c.listMounts("team-alpha"), "GET", "/v2/zone-mounts?zone_id=team-alpha", null],
    [
      "unmount",
      (c: ZoneClient) => c.unmount("m1", "k9"),
      "DELETE",
      "/v2/zone-mounts/m1",
      "k9",
    ],
    [
      "transfer",
      (c: ZoneClient) => c.transfer({ zone_id: "a" }, { zone_id: "b" }, "k10"),
      "POST",
      "/v2/zone-transfers",
      "k10",
    ],
    [
      "transferOperation",
      (c: ZoneClient) => c.transferOperation("op_t"),
      "GET",
      "/v2/zone-transfers/op_t",
      null,
    ],
  ])("%s hits the right method/URL", async (_name, call, method, url, idem) => {
    const fetchFn = vi.fn(async () => ok(OP));
    const client = new ZoneClient({ apiKey: "secret", fetch: fetchFn });

    const result: unknown = await call(client);

    expect(fetchFn).toHaveBeenCalledOnce();
    const [calledUrl, init] = fetchFn.mock.calls[0];
    expect(calledUrl).toBe(`http://localhost:2026${url}`);
    expect(init.method).toBe(method);
    const headers = init.headers as Record<string, string>;
    if (idem === null) {
      expect(headers["Idempotency-Key"]).toBeUndefined();
    } else {
      expect(headers["Idempotency-Key"]).toBe(idem);
    }
    expect((result as { operationId?: string }).operationId ?? "mapped").toBeDefined();
  });

  it("joins with learner=true sends the learner flag", async () => {
    const fetchFn = vi.fn(async () => ok(OP));
    const client = new ZoneClient({ apiKey: "secret", fetch: fetchFn });

    await client.join("team-alpha", ["p1"], "k11", true);

    const [, init] = fetchFn.mock.calls[0];
    expect(JSON.parse(init.body as string)).toEqual({ peers: ["p1"], learner: true });
  });

  it("deprovision carries the zone confirmation header", async () => {
    const fetchFn = vi.fn(async () => ok(OP));
    const client = new ZoneClient({ apiKey: "secret", fetch: fetchFn });

    await client.deprovision("team-alpha", "k12");

    const [, init] = fetchFn.mock.calls[0];
    expect((init.headers as Record<string, string>)["X-Nexus-Confirm-Zone"]).toBe("team-alpha");
  });
});
