import { describe, it, expect } from "bun:test";
import { parseWriteError } from "../../../src/panels/connectors/error-parser.js";

// =============================================================================
// Full structured error (ValidationError.format_message() output)
// =============================================================================

const FULL_VALIDATION_ERROR = `[SCHEMA_VALIDATION_ERROR] Invalid send_email data
Field errors:
  - to: field required
  - subject: field required
See: /.skill/SKILL.md#send-email
Fix:
\`\`\`yaml
to: recipient@example.com
subject: Email subject line
\`\`\``;

const MISSING_INTENT_ERROR = `[MISSING_AGENT_INTENT] Operations require an 'agent_intent' field describing what the agent intends to do
See: /.skill/SKILL.md#required-format
Fix:
\`\`\`yaml
# agent_intent: User requested to send an email to recipient
\`\`\``;

const TRAIT_ERROR = `[MISSING_CONFIRM] This operation requires explicit user confirmation
See: /.skill/SKILL.md#send-email`;

// =============================================================================
// Tests
// =============================================================================

describe("parseWriteError", () => {
  describe("full structured errors", () => {
    it("extracts error code", () => {
      const result = parseWriteError(FULL_VALIDATION_ERROR);
      expect(result.code).toBe("SCHEMA_VALIDATION_ERROR");
    });

    it("extracts message after code", () => {
      const result = parseWriteError(FULL_VALIDATION_ERROR);
      expect(result.message).toBe("Invalid send_email data");
    });

    it("extracts field errors", () => {
      const result = parseWriteError(FULL_VALIDATION_ERROR);
      expect(result.fieldErrors).toHaveLength(2);
      expect(result.fieldErrors[0]).toEqual({ field: "to", error: "field required" });
      expect(result.fieldErrors[1]).toEqual({ field: "subject", error: "field required" });
    });

    it("extracts skill reference", () => {
      const result = parseWriteError(FULL_VALIDATION_ERROR);
      expect(result.skillRef).toBe("/.skill/SKILL.md#send-email");
    });

    it("extracts fix example from code block", () => {
      const result = parseWriteError(FULL_VALIDATION_ERROR);
      expect(result.fixExample).toContain("to: recipient@example.com");
      expect(result.fixExample).toContain("subject: Email subject line");
    });
  });

  describe("missing intent error", () => {
    it("extracts code and message", () => {
      const result = parseWriteError(MISSING_INTENT_ERROR);
      expect(result.code).toBe("MISSING_AGENT_INTENT");
      expect(result.message).toContain("agent_intent");
    });

    it("has no field errors", () => {
      const result = parseWriteError(MISSING_INTENT_ERROR);
      expect(result.fieldErrors).toHaveLength(0);
    });

    it("extracts skill ref", () => {
      const result = parseWriteError(MISSING_INTENT_ERROR);
      expect(result.skillRef).toBe("/.skill/SKILL.md#required-format");
    });

    it("extracts fix example", () => {
      const result = parseWriteError(MISSING_INTENT_ERROR);
      expect(result.fixExample).toContain("agent_intent");
    });
  });

  describe("trait error without fix", () => {
    it("extracts code and message", () => {
      const result = parseWriteError(TRAIT_ERROR);
      expect(result.code).toBe("MISSING_CONFIRM");
      expect(result.message).toContain("user confirmation");
    });

    it("extracts skill ref", () => {
      const result = parseWriteError(TRAIT_ERROR);
      expect(result.skillRef).toBe("/.skill/SKILL.md#send-email");
    });

    it("has null fix example", () => {
      const result = parseWriteError(TRAIT_ERROR);
      expect(result.fixExample).toBeNull();
    });
  });

  describe("plain error strings", () => {
    it("handles simple error with no structure", () => {
      const result = parseWriteError("Permission denied: /mnt/gmail");
      expect(result.code).toBeNull();
      expect(result.message).toBe("Permission denied: /mnt/gmail");
      expect(result.fieldErrors).toHaveLength(0);
      expect(result.skillRef).toBeNull();
      expect(result.fixExample).toBeNull();
    });

    it("handles empty string", () => {
      const result = parseWriteError("");
      expect(result.code).toBeNull();
      expect(result.message).toBe("");
      expect(result.fieldErrors).toHaveLength(0);
    });

    it("handles multi-line plain error", () => {
      const result = parseWriteError("Connection failed\nRetry after 5 seconds");
      expect(result.code).toBeNull();
      expect(result.message).toContain("Connection failed");
    });
  });

  describe("edge cases", () => {
    it("handles field errors with colons in values", () => {
      const error = `[SCHEMA_VALIDATION_ERROR] Invalid data
Field errors:
  - url: must match pattern: https://...
See: /.skill/SKILL.md#test`;
      const result = parseWriteError(error);
      expect(result.fieldErrors).toHaveLength(1);
      expect(result.fieldErrors[0]?.field).toBe("url");
      expect(result.fieldErrors[0]?.error).toContain("must match pattern");
    });

    it("handles error with only See: reference", () => {
      const error = "Something went wrong\nSee: /skills/gmail/SKILL.md";
      const result = parseWriteError(error);
      expect(result.skillRef).toBe("/skills/gmail/SKILL.md");
      expect(result.code).toBeNull();
    });

    it("handles fix without code fences", () => {
      const error = `[FIX_HINT] Missing field
Fix:
# agent_intent: Describe what you want to do`;
      const result = parseWriteError(error);
      expect(result.fixExample).toContain("agent_intent");
    });
  });
});
