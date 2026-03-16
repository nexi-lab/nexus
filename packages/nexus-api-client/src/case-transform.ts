/**
 * Pure utility functions for snake_case ↔ camelCase key transformation.
 *
 * Handles nested objects, arrays, null, and preserves non-plain values (Date, etc.).
 */

/** Convert a single snake_case string to camelCase. */
export function snakeToCamel(str: string): string {
  return str.replace(/_([a-z])/g, (_, letter: string) => letter.toUpperCase());
}

/** Convert a single camelCase string to snake_case. */
export function camelToSnake(str: string): string {
  return str.replace(/[A-Z]/g, (letter) => `_${letter.toLowerCase()}`);
}

/**
 * Recursively transform all keys in an object/array using the given transformer.
 *
 * - Arrays: each element is recursively transformed
 * - null/undefined: returned as-is
 * - Primitives (string, number, boolean): returned as-is
 * - Date: returned as-is (not a plain object)
 * - Plain objects: keys are transformed, values are recursively transformed
 */
export function transformKeys<T>(
  value: unknown,
  transformer: (key: string) => string,
): T {
  if (value === null || value === undefined) {
    return value as T;
  }

  if (Array.isArray(value)) {
    return value.map((item) => transformKeys(item, transformer)) as T;
  }

  if (typeof value !== "object") {
    return value as T;
  }

  // Skip non-plain objects (Date, RegExp, etc.)
  if (Object.getPrototypeOf(value) !== Object.prototype) {
    return value as T;
  }

  const result: Record<string, unknown> = {};
  for (const [key, val] of Object.entries(value as Record<string, unknown>)) {
    result[transformer(key)] = transformKeys(val, transformer);
  }
  return result as T;
}

/** Transform all keys from snake_case to camelCase. */
export function snakeToCamelKeys<T>(value: unknown): T {
  return transformKeys<T>(value, snakeToCamel);
}

/** Transform all keys from camelCase to snake_case. */
export function camelToSnakeKeys<T>(value: unknown): T {
  return transformKeys<T>(value, camelToSnake);
}
