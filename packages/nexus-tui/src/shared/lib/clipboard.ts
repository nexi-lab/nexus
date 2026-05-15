/**
 * Clipboard support via OSC 52 terminal escape sequence.
 * @see Issue #3066, Phase E3
 */

/**
 * Copy text to the system clipboard using OSC 52.
 * Works in terminals that support OSC 52 (most modern terminals).
 * Silently no-ops if the terminal doesn't support it.
 */
export function copyToClipboard(text: string): void {
  const encoded = Buffer.from(text).toString("base64");
  process.stdout.write(`\x1b]52;c;${encoded}\x07`);
}
