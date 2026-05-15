#!/usr/bin/env bun
/**
 * Unwrap useCallback calls: `useCallback(fn, [deps])` → `fn`
 *
 * Handles multi-line useCallback by tracking paren depth.
 */

import { readFileSync, writeFileSync } from "fs";
import { globSync } from "glob";

const ROOT = new URL("../", import.meta.url).pathname;

function unwrapUseCallback(content: string): string {
  let result = "";
  let i = 0;

  while (i < content.length) {
    // Look for `useCallback(`
    const idx = content.indexOf("useCallback(", i);
    if (idx === -1) {
      result += content.slice(i);
      break;
    }

    // Copy everything up to useCallback(
    result += content.slice(i, idx);
    i = idx + "useCallback(".length;

    // Now scan for the matching ) — we need to extract the inner callback
    // which is the first argument. The second argument is optional deps array.
    // Strategy: find the first argument (which is the callback fn), then skip deps.

    // Find the inner content by tracking paren/bracket/brace depth
    let depth = 1; // we already consumed the opening (
    let innerStart = i;
    let firstArgEnd = -1; // position where the callback ends (before , [deps] or before closing ))

    while (i < content.length && depth > 0) {
      const ch = content[i];
      if (ch === "(" || ch === "[" || ch === "{") {
        depth++;
      } else if (ch === ")" || ch === "]" || ch === "}") {
        depth--;
        if (depth === 0) {
          // This is the closing ) of useCallback(...)
          if (firstArgEnd === -1) {
            firstArgEnd = i; // no comma found at depth 1, whole thing is the callback
          }
          i++; // skip closing )
          break;
        }
      } else if (ch === "," && depth === 1 && firstArgEnd === -1) {
        // This comma separates callback from deps array — mark end of callback
        firstArgEnd = i;
      } else if (ch === '"' || ch === "'" || ch === "`") {
        // Skip string literals
        const quote = ch;
        i++;
        while (i < content.length) {
          if (content[i] === "\\" ) {
            i += 2; // skip escape
            continue;
          }
          if (content[i] === quote) {
            i++;
            break;
          }
          i++;
        }
        continue;
      }
      i++;
    }

    // Extract the callback (trim whitespace)
    const callback = content.slice(innerStart, firstArgEnd).trim();

    // Emit just the callback
    result += callback;
  }

  return result;
}

// Fix useRef → use a simple let (just remove the useRef wrapper)
// useRef<T>(initialValue) → initialValue (for refs that hold values)
// But most useRef calls in this codebase store mutable values, so we convert to a variable.
// This is handled separately.

const files = globSync("src/**/*.{tsx,ts}", { cwd: ROOT, absolute: false });

let changed = 0;
for (const relPath of files) {
  const path = ROOT + relPath;
  let content: string;
  try { content = readFileSync(path, "utf8"); } catch { continue; }

  if (!content.includes("useCallback(")) continue;

  const original = content;
  content = unwrapUseCallback(content);

  if (content !== original) {
    writeFileSync(path, content, "utf8");
    console.log(`  ✓ ${relPath}`);
    changed++;
  }
}

console.log(`\nDone. ${changed} files changed.`);
