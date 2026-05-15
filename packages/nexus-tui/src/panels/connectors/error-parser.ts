/**
 * Parse structured validation errors from backend connector write responses.
 *
 * The backend SkillErrorFormatter produces errors in a predictable format:
 *
 *   [CODE] message
 *   Field errors:
 *     - field_name: error message
 *   See: /.skill/SKILL.md#section
 *   Fix:
 *   ```yaml
 *   field: corrected_value
 *   ```
 *
 * This parser extracts those sections into structured data for the TUI to
 * render with color-coded field errors and actionable fix hints.
 */

// =============================================================================
// Types
// =============================================================================

export interface ParsedWriteError {
  /** Error code (e.g., "SCHEMA_VALIDATION_ERROR", "MISSING_AGENT_INTENT") */
  readonly code: string | null;
  /** Human-readable error message */
  readonly message: string;
  /** Field-level validation errors */
  readonly fieldErrors: readonly FieldError[];
  /** Skill doc reference path (e.g., "/.skill/SKILL.md#required-format") */
  readonly skillRef: string | null;
  /** YAML fix example (code block content, without fences) */
  readonly fixExample: string | null;
}

export interface FieldError {
  readonly field: string;
  readonly error: string;
}

// =============================================================================
// Parser
// =============================================================================

/**
 * Parse a backend error string into structured error data.
 *
 * Handles both the structured ValidationError format and plain error strings.
 */
export function parseWriteError(errorString: string): ParsedWriteError {
  // Extract error code: [CODE] message
  const codeMatch = errorString.match(/^\[(\w+)]\s*(.+?)(?:\n|$)/);
  const code = codeMatch?.[1] ?? null;
  const messageAfterCode = codeMatch?.[2] ?? null;

  // Extract field errors section
  const fieldErrors: FieldError[] = [];
  const fieldSectionMatch = errorString.match(
    /Field errors:\s*\n((?:\s*-\s*.+\n?)+)/i,
  );
  if (fieldSectionMatch?.[1]) {
    const fieldLines = fieldSectionMatch[1].split("\n");
    for (const line of fieldLines) {
      const fieldMatch = line.match(/^\s*-\s*(\S+):\s*(.+)$/);
      if (fieldMatch?.[1] && fieldMatch[2]) {
        fieldErrors.push({ field: fieldMatch[1], error: fieldMatch[2].trim() });
      }
    }
  }

  // Extract skill doc reference: See: path#section
  const seeMatch = errorString.match(/See:\s*(\S+)/i);
  const skillRef = seeMatch?.[1] ?? null;

  // Extract fix example: everything between ```yaml and ```
  let fixExample: string | null = null;
  const fixBlockMatch = errorString.match(
    /Fix:\s*\n```(?:yaml)?\s*\n([\s\S]*?)```/i,
  );
  if (fixBlockMatch?.[1]) {
    fixExample = fixBlockMatch[1].trim();
  } else {
    // Try simpler format: Fix:\n# content (no code fences)
    const simpleFix = errorString.match(/Fix:\s*\n((?:#?\s*.+\n?)+)/i);
    if (simpleFix?.[1]) {
      fixExample = simpleFix[1].trim();
    }
  }

  // Build the primary message — use the part after [CODE] if available,
  // otherwise use the first line of the error string
  let message: string;
  if (messageAfterCode) {
    message = messageAfterCode;
  } else {
    // Take everything before "Field errors:" or "See:" or "Fix:"
    const firstSectionIdx = Math.min(
      ...[
        errorString.indexOf("Field errors:"),
        errorString.indexOf("See:"),
        errorString.indexOf("Fix:"),
      ]
        .filter((i) => i >= 0)
        .concat([errorString.length]),
    );
    message = errorString.substring(0, firstSectionIdx).trim();
  }

  return {
    code,
    message,
    fieldErrors,
    skillRef,
    fixExample,
  };
}
