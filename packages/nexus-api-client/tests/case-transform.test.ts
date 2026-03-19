import { describe, it, expect } from "vitest";
import {
  snakeToCamel,
  camelToSnake,
  transformKeys,
  snakeToCamelKeys,
  camelToSnakeKeys,
} from "../src/case-transform.js";

describe("snakeToCamel", () => {
  it("converts simple snake_case", () => {
    expect(snakeToCamel("from_agent")).toBe("fromAgent");
  });

  it("converts multiple underscores", () => {
    expect(snakeToCamel("created_at_utc")).toBe("createdAtUtc");
  });

  it("leaves single words unchanged", () => {
    expect(snakeToCamel("name")).toBe("name");
  });

  it("leaves already camelCase unchanged", () => {
    expect(snakeToCamel("fromAgent")).toBe("fromAgent");
  });

  it("handles leading underscores", () => {
    // Leading underscores followed by lowercase are transformed
    expect(snakeToCamel("_private")).toBe("Private");
  });

  it("handles empty string", () => {
    expect(snakeToCamel("")).toBe("");
  });

  it("handles strings with digits", () => {
    expect(snakeToCamel("x402_config")).toBe("x402Config");
  });
});

describe("camelToSnake", () => {
  it("converts simple camelCase", () => {
    expect(camelToSnake("fromAgent")).toBe("from_agent");
  });

  it("converts multiple uppercase letters", () => {
    expect(camelToSnake("createdAtUtc")).toBe("created_at_utc");
  });

  it("leaves single words unchanged", () => {
    expect(camelToSnake("name")).toBe("name");
  });

  it("leaves already snake_case unchanged", () => {
    expect(camelToSnake("from_agent")).toBe("from_agent");
  });

  it("handles empty string", () => {
    expect(camelToSnake("")).toBe("");
  });
});

describe("transformKeys", () => {
  it("transforms object keys", () => {
    const input = { from_agent: "a", to_agent: "b" };
    const result = transformKeys<Record<string, string>>(input, snakeToCamel);
    expect(result).toEqual({ fromAgent: "a", toAgent: "b" });
  });

  it("transforms nested objects recursively", () => {
    const input = { outer_key: { inner_key: "value" } };
    const result = transformKeys(input, snakeToCamel);
    expect(result).toEqual({ outerKey: { innerKey: "value" } });
  });

  it("transforms arrays of objects", () => {
    const input = [{ from_agent: "a" }, { from_agent: "b" }];
    const result = transformKeys(input, snakeToCamel);
    expect(result).toEqual([{ fromAgent: "a" }, { fromAgent: "b" }]);
  });

  it("handles null", () => {
    expect(transformKeys(null, snakeToCamel)).toBeNull();
  });

  it("handles undefined", () => {
    expect(transformKeys(undefined, snakeToCamel)).toBeUndefined();
  });

  it("handles primitives", () => {
    expect(transformKeys("hello", snakeToCamel)).toBe("hello");
    expect(transformKeys(42, snakeToCamel)).toBe(42);
    expect(transformKeys(true, snakeToCamel)).toBe(true);
  });

  it("preserves Date objects", () => {
    const date = new Date("2025-01-01");
    expect(transformKeys(date, snakeToCamel)).toBe(date);
  });

  it("handles empty objects", () => {
    expect(transformKeys({}, snakeToCamel)).toEqual({});
  });

  it("handles deeply nested arrays", () => {
    const input = { items: [{ sub_items: [{ deep_key: 1 }] }] };
    const result = transformKeys(input, snakeToCamel);
    expect(result).toEqual({ items: [{ subItems: [{ deepKey: 1 }] }] });
  });

  it("handles mixed null values in objects", () => {
    const input = { some_key: null, other_key: "val" };
    const result = transformKeys(input, snakeToCamel);
    expect(result).toEqual({ someKey: null, otherKey: "val" });
  });
});

describe("snakeToCamelKeys", () => {
  it("is a convenience wrapper", () => {
    const input = { from_agent: "a" };
    expect(snakeToCamelKeys(input)).toEqual({ fromAgent: "a" });
  });
});

describe("camelToSnakeKeys", () => {
  it("is a convenience wrapper", () => {
    const input = { fromAgent: "a" };
    expect(camelToSnakeKeys(input)).toEqual({ from_agent: "a" });
  });
});
