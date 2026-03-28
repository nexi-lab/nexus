/**
 * Schema-to-YAML template generator.
 *
 * Pure function that converts an annotated schema (YAML string from the API)
 * into a pre-filled YAML template for the Write tab.
 *
 * The schema format is annotated YAML with comments like:
 *   field_name:           # (required) Description — type: string
 *   optional_field:       # (optional) Description — type: integer, default: 0
 */

// =============================================================================
// Types
// =============================================================================

export interface SchemaField {
  readonly name: string;
  readonly type: string;
  readonly required: boolean;
  readonly description: string;
  readonly default_value: string | null;
  readonly enum_values: readonly string[];
  readonly is_nested: boolean;
  readonly children: readonly SchemaField[];
}

// =============================================================================
// Parser: extract fields from annotated schema YAML
// =============================================================================

/**
 * Parse an annotated schema string into structured fields.
 *
 * Handles formats like:
 *   field_name:           # (required) Description — type: string
 *   field_name: value     # (optional) Description — type: integer
 *   field_name:           # (required) One of: val1, val2, val3
 */
export function parseSchemaFields(schemaContent: string): readonly SchemaField[] {
  const lines = schemaContent.split("\n");
  const fields: SchemaField[] = [];
  let currentIndent = -1;

  for (const line of lines) {
    // Skip empty lines and pure comment lines (not field comments)
    const trimmed = line.trim();
    if (!trimmed || (trimmed.startsWith("#") && !trimmed.includes(":"))) continue;

    // Match field pattern: name: [value] # comment
    const fieldMatch = trimmed.match(
      /^(\w[\w.]*)\s*:\s*(.*?)(?:\s*#\s*(.*))?$/,
    );
    if (!fieldMatch) continue;

    const [, name, rawValue, comment] = fieldMatch;
    if (!name) continue;

    // Parse required/optional from comment
    const isRequired = comment ? /\(required\)/i.test(comment) : false;

    // Parse type from comment
    const typeMatch = comment?.match(/type:\s*(\w+)/i);
    const type = typeMatch?.[1] ?? "string";

    // Parse description — everything before "type:" or "One of:" or "default:"
    let description = comment ?? "";
    description = description
      .replace(/\(required\)/i, "")
      .replace(/\(optional\)/i, "")
      .replace(/type:\s*\w+/i, "")
      .replace(/default:\s*\S+/i, "")
      .replace(/One of:.*$/i, "")
      .replace(/[—–-]\s*$/, "")
      .trim();

    // Parse default value
    const defaultMatch = comment?.match(/default:\s*(\S+)/i);
    const defaultValue = defaultMatch?.[1] ?? null;

    // Parse enum values — strip trailing "default: X" from the match
    const enumMatch = comment?.match(/One of:\s*(.+)$/i);
    const enumValues: string[] = enumMatch
      ? enumMatch[1]
          .replace(/,?\s*default:\s*\S+/i, "")
          .split(",")
          .map((v) => v.trim())
          .filter(Boolean)
      : [];

    // Determine nesting from indent
    const indent = line.search(/\S/);
    const valueStr = rawValue.trim();
    const isNested = valueStr === "" && !comment?.includes("type:");

    fields.push({
      name,
      type,
      required: isRequired,
      description,
      default_value: defaultValue,
      enum_values: enumValues,
      is_nested: isNested,
      children: [],
    });
  }

  return fields;
}

// =============================================================================
// Template generator
// =============================================================================

/**
 * Generate a YAML template from a schema string.
 *
 * Required fields are shown with placeholder values.
 * Optional fields are commented out with their defaults.
 * Enum fields show valid values as comments.
 */
export function generateTemplate(
  schemaContent: string,
  operationName: string,
): string {
  const fields = parseSchemaFields(schemaContent);

  if (fields.length === 0) {
    // If we can't parse structured fields, return the schema as-is
    // with a header comment — the user can edit it directly
    return `# ${operationName}\n# Edit the fields below:\n\n${schemaContent}`;
  }

  const lines: string[] = [];
  lines.push(`# ${operationName}`);
  lines.push(`# Required fields are pre-filled. Optional fields are commented out.`);
  lines.push("");

  for (const field of fields) {
    if (field.is_nested) {
      // Section header
      if (field.required) {
        lines.push(`${field.name}:`);
      } else {
        lines.push(`# ${field.name}:`);
      }
      continue;
    }

    const enumComment = field.enum_values.length > 0
      ? `  # One of: ${field.enum_values.join(", ")}`
      : "";

    const descComment = field.description
      ? `  # ${field.description}`
      : "";

    const placeholder = getPlaceholder(field);

    if (field.required) {
      lines.push(`${field.name}: ${placeholder}${enumComment || descComment}`);
    } else {
      // Optional fields are commented out
      const value = field.default_value ?? placeholder;
      lines.push(`# ${field.name}: ${value}${enumComment || descComment}`);
    }
  }

  return lines.join("\n") + "\n";
}

/**
 * Get a sensible placeholder for a field based on its type.
 */
function getPlaceholder(field: SchemaField): string {
  if (field.default_value) return field.default_value;
  if (field.enum_values.length > 0) return field.enum_values[0];

  switch (field.type.toLowerCase()) {
    case "string":
      return `"<${field.name}>"`;
    case "integer":
    case "int":
      return "0";
    case "number":
    case "float":
      return "0.0";
    case "boolean":
    case "bool":
      return "false";
    case "array":
    case "list":
      return "[]";
    case "object":
    case "dict":
      return "{}";
    default:
      return `"<${field.name}>"`;
  }
}

/**
 * Generate a template directly from an operation name and raw schema content.
 *
 * Convenience wrapper used by the Write tab.
 */
export function generateWriteTemplate(
  operationName: string,
  schemaContent: string,
): string {
  return generateTemplate(schemaContent, operationName);
}
