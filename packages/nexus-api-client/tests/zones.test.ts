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
});
