/**
 * Text truncation utility for terminal column alignment.
 *
 * @see Issue #3102, Decision 8A
 */

export function truncateText(text: string, maxLen: number): string {
  if (text.length <= maxLen) return text;
  return `${text.slice(0, maxLen - 3)}...`;
}
