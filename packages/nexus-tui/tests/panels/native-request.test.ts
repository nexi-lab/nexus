import { describe, expect, it } from "bun:test";
import { parseNativeRequest } from "../../src/panels/api-console/native-request.js";

describe("native Console request boundary", () => {
  it("preserves full relationship deletion bodies and Unicode paths", () => {
    const tuple = { zone: "shared", object_type: "file", object_id: "/日本語", relation: "viewer", subject_type: "user", subject_id: "alice", subject_relation: null };
    const parsed = parseNativeRequest(`DELETE /v2/rebac/tuples ${JSON.stringify(tuple)}`);
    expect(parsed.method).toBe("DELETE");
    expect(JSON.parse(parsed.body!)).toEqual(tuple);
    expect(parsed.contentType).toBe("application/json");
    expect(parseNativeRequest("PUT /v2/files?path=%2Fnotes raw file content").body).toBe("raw file content");
  });
  for (const invalid of ["GET https://example.com/v2/auth/keys", "GET /api/v2/pay/balance", "GET /v2/../api/v2/pay", "GET /v2/auth/keys#fragment", "GET /v2/status {}", "POST /v2/auth/keys invalid-json"]) {
    it(`rejects invalid destinations and bodies: ${invalid}`, () => expect(() => parseNativeRequest(invalid)).toThrow());
  }
});
