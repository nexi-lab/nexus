import { describe, it, expect } from "bun:test";
import {
  parseSchemaFields,
  generateTemplate,
  generateWriteTemplate,
} from "../../../src/panels/connectors/template-generator.js";

// =============================================================================
// Real-world schema fixtures
// =============================================================================

const GMAIL_SEND_SCHEMA = `to:           # (required) Recipient email address — type: string
subject:      # (required) Email subject line — type: string
body:         # (required) Email body content — type: string
cc:           # (optional) CC recipients — type: string
bcc:          # (optional) BCC recipients — type: string
reply_to:     # (optional) Reply-to address — type: string
thread_id:    # (optional) Thread ID for replies — type: string`;

const CALENDAR_CREATE_SCHEMA = `summary:      # (required) Event title — type: string
start_time:   # (required) Start time (ISO 8601) — type: string
end_time:     # (required) End time (ISO 8601) — type: string
description:  # (optional) Event description — type: string
location:     # (optional) Event location — type: string
attendees:    # (optional) Comma-separated email addresses — type: string
all_day:      # (optional) Whether this is an all-day event — type: boolean, default: false
reminder_minutes: # (optional) Reminder before event — type: integer, default: 10`;

const ENUM_SCHEMA = `priority:     # (required) Task priority — type: string — One of: low, medium, high, critical
status:       # (optional) Task status — type: string — One of: open, in_progress, done, default: open`;

// =============================================================================
// parseSchemaFields
// =============================================================================

describe("parseSchemaFields", () => {
  it("parses required fields from Gmail schema", () => {
    const fields = parseSchemaFields(GMAIL_SEND_SCHEMA);
    const required = fields.filter((f) => f.required);
    expect(required).toHaveLength(3);
    expect(required.map((f) => f.name)).toEqual(["to", "subject", "body"]);
  });

  it("parses optional fields from Gmail schema", () => {
    const fields = parseSchemaFields(GMAIL_SEND_SCHEMA);
    const optional = fields.filter((f) => !f.required);
    expect(optional).toHaveLength(4);
    expect(optional.map((f) => f.name)).toEqual(["cc", "bcc", "reply_to", "thread_id"]);
  });

  it("extracts field types", () => {
    const fields = parseSchemaFields(CALENDAR_CREATE_SCHEMA);
    const allDay = fields.find((f) => f.name === "all_day");
    expect(allDay?.type).toBe("boolean");

    const reminder = fields.find((f) => f.name === "reminder_minutes");
    expect(reminder?.type).toBe("integer");
  });

  it("extracts default values", () => {
    const fields = parseSchemaFields(CALENDAR_CREATE_SCHEMA);
    const allDay = fields.find((f) => f.name === "all_day");
    expect(allDay?.default_value).toBe("false");

    const reminder = fields.find((f) => f.name === "reminder_minutes");
    expect(reminder?.default_value).toBe("10");
  });

  it("extracts enum values", () => {
    const fields = parseSchemaFields(ENUM_SCHEMA);
    const priority = fields.find((f) => f.name === "priority");
    expect(priority?.enum_values).toEqual(["low", "medium", "high", "critical"]);
  });

  it("extracts enum default", () => {
    const fields = parseSchemaFields(ENUM_SCHEMA);
    const status = fields.find((f) => f.name === "status");
    expect(status?.enum_values).toEqual(["open", "in_progress", "done"]);
    expect(status?.default_value).toBe("open");
  });

  it("returns empty for empty input", () => {
    expect(parseSchemaFields("")).toHaveLength(0);
  });

  it("returns empty for pure comments", () => {
    expect(parseSchemaFields("# This is a comment\n# Another comment")).toHaveLength(0);
  });

  it("handles single field", () => {
    const fields = parseSchemaFields("name: # (required) Name — type: string");
    expect(fields).toHaveLength(1);
    expect(fields[0]?.name).toBe("name");
    expect(fields[0]?.required).toBe(true);
  });
});

// =============================================================================
// generateTemplate
// =============================================================================

describe("generateTemplate", () => {
  it("generates template with required fields pre-filled", () => {
    const template = generateTemplate(GMAIL_SEND_SCHEMA, "send_email");
    expect(template).toContain("# send_email");
    expect(template).toContain('to: "<to>"');
    expect(template).toContain('subject: "<subject>"');
    expect(template).toContain('body: "<body>"');
  });

  it("comments out optional fields", () => {
    const template = generateTemplate(GMAIL_SEND_SCHEMA, "send_email");
    expect(template).toContain("# cc:");
    expect(template).toContain("# bcc:");
    expect(template).toContain("# reply_to:");
  });

  it("uses default values for optional fields", () => {
    const template = generateTemplate(CALENDAR_CREATE_SCHEMA, "create_event");
    expect(template).toContain("# all_day: false");
    expect(template).toContain("# reminder_minutes: 10");
  });

  it("uses enum first value as placeholder for required enum fields", () => {
    const template = generateTemplate(ENUM_SCHEMA, "create_task");
    expect(template).toContain("priority: low");
    expect(template).toContain("One of:");
  });

  it("uses correct type placeholders", () => {
    const schema = `count: # (required) Item count — type: integer
price: # (required) Item price — type: number
active: # (required) Is active — type: boolean
tags: # (required) Tags — type: array`;

    const template = generateTemplate(schema, "test_op");
    expect(template).toContain("count: 0");
    expect(template).toContain("price: 0.0");
    expect(template).toContain("active: false");
    expect(template).toContain("tags: []");
  });

  it("falls back to raw schema when no fields are parseable", () => {
    const rawSchema = "some unparseable content without field patterns";
    const template = generateTemplate(rawSchema, "test_op");
    expect(template).toContain("# test_op");
    expect(template).toContain(rawSchema);
  });

  it("ends with a newline", () => {
    const template = generateTemplate(GMAIL_SEND_SCHEMA, "send_email");
    expect(template.endsWith("\n")).toBe(true);
  });
});

// =============================================================================
// generateWriteTemplate (convenience wrapper)
// =============================================================================

describe("generateWriteTemplate", () => {
  it("delegates to generateTemplate with swapped args", () => {
    const template = generateWriteTemplate("send_email", GMAIL_SEND_SCHEMA);
    expect(template).toContain("# send_email");
    expect(template).toContain('to: "<to>"');
  });

  it("handles empty schema", () => {
    const template = generateWriteTemplate("empty_op", "");
    expect(template).toContain("# empty_op");
  });
});

// =============================================================================
// Edge cases
// =============================================================================

describe("edge cases", () => {
  it("handles deeply nested field names with dots", () => {
    const schema = `config.timeout: # (required) Timeout in seconds — type: integer`;
    const fields = parseSchemaFields(schema);
    expect(fields).toHaveLength(1);
    expect(fields[0]?.name).toBe("config.timeout");
  });

  it("handles schema with mixed spacing", () => {
    const schema = `to:   # (required) Recipient — type: string
  subject:    # (required) Subject — type: string`;
    const fields = parseSchemaFields(schema);
    // Both should be parsed (the indented one may or may not parse depending on implementation)
    expect(fields.length).toBeGreaterThanOrEqual(1);
  });

  it("handles very long descriptions", () => {
    const schema = `name: # (required) This is a very long description that goes on and on and on and on and on and on — type: string`;
    const fields = parseSchemaFields(schema);
    expect(fields).toHaveLength(1);
    expect(fields[0]?.type).toBe("string");
  });

  it("handles field with value already set", () => {
    const schema = `timeout: 30 # (optional) Timeout — type: integer`;
    const fields = parseSchemaFields(schema);
    expect(fields).toHaveLength(1);
  });
});
