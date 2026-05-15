#!/usr/bin/env bun
/**
 * Fix SolidJS signal accessor calls.
 *
 * Parses TypeScript error output for Accessor<T> patterns and adds `()`
 * after the signal identifier at each error location.
 *
 * Also handles:
 * - useCallback unwrapping (simple cases)
 * - Missing `Setter` imports from solid-js
 */

import { readFileSync, writeFileSync } from "fs";
import { spawnSync } from "child_process";
import { join } from "path";

const ROOT = new URL("../", import.meta.url).pathname;

// ─────────────────────────────────────────────────────────────────────────────
// Step 1: Run tsc and parse errors
// ─────────────────────────────────────────────────────────────────────────────

function runTsc(): string {
  const result = spawnSync("bun", ["run", "lint"], {
    cwd: ROOT,
    encoding: "utf8",
    maxBuffer: 10 * 1024 * 1024,
  });
  return result.stdout + result.stderr;
}

interface TscError {
  file: string;
  line: number; // 1-based
  col: number;  // 1-based
  code: number;
  message: string;
}

function parseErrors(output: string): TscError[] {
  const errors: TscError[] = [];
  const regex = /^(.+?)\((\d+),(\d+)\): error TS(\d+): (.+)$/gm;
  let m;
  while ((m = regex.exec(output)) !== null) {
    errors.push({
      file: join(ROOT, m[1]!.replace(/^(src|tests)/, "$1")),
      line: parseInt(m[2]!, 10),
      col: parseInt(m[3]!, 10),
      code: parseInt(m[4]!, 10),
      message: m[5]!,
    });
  }
  return errors;
}

// ─────────────────────────────────────────────────────────────────────────────
// Step 2: Categorize which errors need `()` insertion
// ─────────────────────────────────────────────────────────────────────────────

function needsSignalCall(err: TscError): boolean {
  const msg = err.message;
  return (
    msg.includes("Accessor<") ||
    msg.includes("'Accessor<") ||
    (msg.includes("always return true") && msg.includes("function is always defined"))
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Step 3: Insert `()` at the right position in a line
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Given a line of source and a 1-based column pointing to either:
 *   - A property access: `signal.prop` — col points to `prop`
 *   - A comparison: `signal === x` or `signal >= x` — col points to `signal`
 *   - An assignment: `fn(signal)` — col points to `signal`
 *   - A condition: `if (signal)` — col points to `signal`
 *
 * Returns the line with `()` inserted after the identifier.
 */
function insertSignalCall(line: string, col: number, errorMsg: string): string {
  // col is 1-based
  const idx = col - 1; // 0-based

  // Case 1: property access — col points to property name AFTER the dot
  // e.g. "  name.trim()" with col pointing to 't' of 'trim'
  // We need to add () before the '.'
  if (
    errorMsg.includes("Property '") &&
    errorMsg.includes("does not exist on type 'Accessor<")
  ) {
    // Find the '.' that comes just before col
    let dotIdx = idx - 1;
    while (dotIdx >= 0 && line[dotIdx] === " ") dotIdx--;
    if (dotIdx >= 0 && line[dotIdx] === ".") {
      // Check there's no `()` already before the dot
      let idEnd = dotIdx;
      // Find end of identifier before the dot (skip backwards over identifier chars)
      let idStart = idEnd - 1;
      while (idStart >= 0 && /[\w$]/.test(line[idStart]!)) idStart--;
      idStart++;
      // Check that it's not already called: `name().prop`
      if (idStart > 0 && line[idStart - 1] === ")") return line; // already called
      return line.slice(0, dotIdx) + "()" + line.slice(dotIdx);
    }
    return line;
  }

  // Case 2: the error col points directly to the accessor identifier
  // Find the identifier that starts at or contains col
  let start = idx;
  // Walk back to start of identifier
  while (start > 0 && /[\w$]/.test(line[start - 1]!)) start--;
  // Walk forward to end of identifier
  let end = idx;
  while (end < line.length && /[\w$]/.test(line[end]!)) end++;

  if (start >= end) return line; // nothing found

  const identifier = line.slice(start, end);
  if (!identifier) return line;

  // If identifier is already followed by `(`, it's already called
  const afterIdent = line.slice(end).trimStart();
  if (afterIdent.startsWith("(")) return line;

  // If preceded by `.`, it's a method — don't touch
  const beforeIdent = line.slice(0, start);
  if (beforeIdent.trimEnd().endsWith(".")) return line;

  // Insert () after the identifier
  return line.slice(0, end) + "()" + line.slice(end);
}

// ─────────────────────────────────────────────────────────────────────────────
// Step 4: Apply fixes file by file
// ─────────────────────────────────────────────────────────────────────────────

function applySignalFixes(errors: TscError[]): number {
  // Group by file
  const byFile = new Map<string, TscError[]>();
  for (const e of errors) {
    if (!needsSignalCall(e)) continue;
    if (!byFile.has(e.file)) byFile.set(e.file, []);
    byFile.get(e.file)!.push(e);
  }

  let fixed = 0;
  for (const [filePath, fileErrors] of byFile) {
    let content: string;
    try { content = readFileSync(filePath, "utf8"); } catch { continue; }

    const lines = content.split("\n");

    // Process errors from BOTTOM to TOP so line numbers stay valid
    const sorted = [...fileErrors].sort((a, b) => b.line - a.line || b.col - a.col);

    let changed = false;
    const processed = new Set<string>();

    for (const err of sorted) {
      const key = `${err.line}:${err.col}`;
      if (processed.has(key)) continue;
      processed.add(key);

      const lineIdx = err.line - 1;
      if (lineIdx < 0 || lineIdx >= lines.length) continue;

      const original = lines[lineIdx]!;
      const updated = insertSignalCall(original, err.col, err.message);

      if (updated !== original) {
        lines[lineIdx] = updated;
        changed = true;
        fixed++;
      }
    }

    if (changed) {
      writeFileSync(filePath, lines.join("\n"), "utf8");
    }
  }

  return fixed;
}

// ─────────────────────────────────────────────────────────────────────────────
// Step 5: Unwrap useCallback (simple patterns)
// ─────────────────────────────────────────────────────────────────────────────

function unwrapUseCallbacks(): number {
  const output = runTsc();
  const callbackErrors: TscError[] = [];

  const regex = /^(.+?)\((\d+),(\d+)\): error TS2304: Cannot find name 'useCallback'\.$/gm;
  let m;
  while ((m = regex.exec(output)) !== null) {
    callbackErrors.push({
      file: join(ROOT, m[1]!),
      line: parseInt(m[2]!, 10),
      col: parseInt(m[3]!, 10),
      code: 2304,
      message: "Cannot find name 'useCallback'.",
    });
  }

  const byFile = new Map<string, TscError[]>();
  for (const e of callbackErrors) {
    if (!byFile.has(e.file)) byFile.set(e.file, []);
    byFile.get(e.file)!.push(e);
  }

  let fixed = 0;
  for (const [filePath] of byFile) {
    let content: string;
    try { content = readFileSync(filePath, "utf8"); } catch { continue; }

    const original = content;

    // Pattern 1: const fn = useCallback((...) => expr, [deps]);
    // Single-line arrow function with deps array
    content = content.replace(
      /\buseCallback\(\s*((?:\([^)]*\)|[^,])+?)\s*,\s*\[[^\]]*\]\s*\)/g,
      (_, fn) => fn.trim(),
    );

    // Pattern 2: useCallback(() => { ... }) — no deps (test-only or component)
    // We'll just strip useCallback( prefix and the final ) if the structure is clear
    // This is handled above if deps array is empty or a variable

    // Pattern 3: useCallback wrapping an existing named function reference
    content = content.replace(/\buseCallback\((\w+),\s*\[[^\]]*\]\)/g, "$1");

    if (content !== original) {
      writeFileSync(filePath, content, "utf8");
      fixed++;
    }
  }

  return fixed;
}

// ─────────────────────────────────────────────────────────────────────────────
// Step 6: Fix missing Setter import
// ─────────────────────────────────────────────────────────────────────────────

function fixSetterImports(): number {
  const output = runTsc();
  const setterErrors = output.match(/^(.+?)\(\d+,\d+\): error TS2304: Cannot find name 'Setter'\./gm);
  if (!setterErrors) return 0;

  // Extract unique files
  const files = new Set<string>();
  for (const e of setterErrors) {
    const m = e.match(/^(.+?)\(\d+,/);
    if (m) files.add(join(ROOT, m[1]!));
  }

  let fixed = 0;
  for (const filePath of files) {
    let content: string;
    try { content = readFileSync(filePath, "utf8"); } catch { continue; }

    if (/\bSetter\b/.test(content) && !content.includes('"Setter"') && !content.includes("'Setter'")) {
      // Add Setter to solid-js import
      const updated = content.replace(
        /^(import\s+\{)([^}]+)(\}\s*from\s*["']solid-js["'])/m,
        (_, open, names, close) => {
          if (names.includes("Setter")) return _ ;
          return `${open}${names.trimEnd()}, Setter ${close}`;
        },
      );
      if (updated !== content) {
        writeFileSync(filePath, updated, "utf8");
        fixed++;
      }
    }
  }
  return fixed;
}

// ─────────────────────────────────────────────────────────────────────────────
// Main
// ─────────────────────────────────────────────────────────────────────────────

const args = process.argv.slice(2);
const maxRounds = parseInt(args.find(a => a.startsWith("--rounds="))?.split("=")[1] ?? "5", 10);

console.log("=== Step 1: Fix missing Setter imports ===");
const setterFixed = fixSetterImports();
console.log(`  Fixed Setter imports in ${setterFixed} files\n`);

console.log("=== Step 2: Unwrap useCallback ===");
const callbackFixed = unwrapUseCallbacks();
console.log(`  Unwrapped useCallback in ${callbackFixed} files\n`);

console.log("=== Step 3: Fix signal accessor calls (iterative) ===");
for (let round = 1; round <= maxRounds; round++) {
  const output = runTsc();
  const errors = parseErrors(output);
  const accessorErrors = errors.filter(needsSignalCall);

  console.log(`  Round ${round}: ${accessorErrors.length} accessor errors found`);
  if (accessorErrors.length === 0) {
    console.log("  No more accessor errors. Done.");
    break;
  }

  const fixed = applySignalFixes(accessorErrors);
  console.log(`  Fixed ${fixed} sites\n`);

  if (fixed === 0) {
    console.log("  No progress made — remaining errors need manual fixing.");
    break;
  }
}

console.log("\n=== Final error count ===");
const finalOutput = runTsc();
const finalErrors = parseErrors(finalOutput);
const remaining = finalErrors.filter(needsSignalCall);
console.log(`  Remaining accessor errors: ${remaining.length}`);
console.log(`  Total TS errors: ${finalErrors.length}`);
