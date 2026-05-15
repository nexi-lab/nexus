#!/usr/bin/env bun
/**
 * Mechanical SolidJS migration for Nexus TUI.
 *
 * Handles:
 *   1. React imports → solid-js equivalents
 *   2. @opentui/react → @opentui/solid
 *   3. Hook renames (useState→createSignal, useEffect→createEffect, useMemo→createMemo)
 *   4. Removal of useEffect/useMemo/createEffect/createMemo deps arrays (simple cases)
 *   5. React.ReactNode → JSX.Element
 *   6. React.lazy → lazy (solid-js)
 *
 * Does NOT handle (manual work):
 *   - Store migrations (Zustand → createStore)
 *   - useSwr → createResource
 *   - useKeyboard rewrite
 *   - Props destructuring → props.field
 *   - .map() → <For>
 *   - Conditional && → <Show>
 *   - useCallback unwrapping (import removed → TS errors guide manual fix)
 *   - useRef (import removed → TS errors guide manual fix)
 */

import { readdirSync, readFileSync, writeFileSync, statSync } from "fs";
import { join, extname, relative } from "path";

// ─────────────────────────────────────────────
// Config
// ─────────────────────────────────────────────

const ROOT = new URL("../", import.meta.url).pathname;
const DIRS = [join(ROOT, "src"), join(ROOT, "tests")];

// React named import → solid-js equivalent (null = remove from import, no replacement)
const HOOK_RENAME: Record<string, string | null> = {
  useState: "createSignal",
  useEffect: "createEffect",
  useLayoutEffect: "createEffect",
  useMemo: "createMemo",
  useContext: "useContext",
  createContext: "createContext",
  lazy: "lazy",
  Suspense: "Suspense",
  // These intentionally removed — TypeScript errors will guide manual fixes
  useCallback: null,
  useRef: null,
  useReducer: null,
  useImperativeHandle: null,
  forwardRef: null,
  memo: null,
  Fragment: null,
};

// @opentui/react exported name → @opentui/solid name
const OPENTUI_RENAME: Record<string, string> = {
  createRoot: "render", // API change — index.tsx needs manual update
  useKeyboard: "useKeyboard",
  useRenderer: "useRenderer",
  useTerminalDimensions: "useTerminalDimensions",
  usePaste: "usePaste",
  onFocus: "onFocus",
  onBlur: "onBlur",
  onResize: "onResize",
  useTimeline: "useTimeline",
  testRender: "testRender",
};

// ─────────────────────────────────────────────
// File discovery
// ─────────────────────────────────────────────

function getAllFiles(dir: string): string[] {
  const result: string[] = [];
  function walk(d: string) {
    let entries: string[];
    try { entries = readdirSync(d); } catch { return; }
    for (const e of entries) {
      const full = join(d, e);
      let s;
      try { s = statSync(full); } catch { continue; }
      if (s.isDirectory()) {
        if (e !== "node_modules" && e !== "dist") walk(full);
      } else if ([".ts", ".tsx"].includes(extname(full))) {
        result.push(full);
      }
    }
  }
  walk(dir);
  return result;
}

// ─────────────────────────────────────────────
// Transform helpers
// ─────────────────────────────────────────────

/**
 * Parse `import X, { a, b as c } from "mod"` into its parts.
 */
function parseImportLine(line: string): {
  defaultImport: string | null;
  namedImports: Array<{ local: string; imported: string }>;
  isTypeOnly: boolean;
} | null {
  // Match: import [type] [Default,] { Named } from "mod"
  // or:    import [type] Default from "mod"
  const m = line.match(
    /^import\s+(type\s+)?(?:(\w+)\s*,?\s*)?(?:\{([^}]*)\})?\s*from\s*["']([^"']+)["']/,
  );
  if (!m) return null;

  const isTypeOnly = Boolean(m[1]);
  const defaultImport = m[2] ?? null;
  const namedStr = m[3] ?? "";
  const namedImports = namedStr
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .map((s) => {
      const parts = s.split(/\s+as\s+/);
      return { imported: parts[0]!.trim(), local: (parts[1] ?? parts[0]!).trim() };
    });

  return { defaultImport, namedImports, isTypeOnly };
}

/**
 * Transform a React import line into solid-js import(s).
 * Returns replacement lines (may be empty string to delete the line).
 */
function transformReactImport(line: string): string {
  const parsed = parseImportLine(line);
  if (!parsed) return line;

  const solidImports: string[] = [];

  // Map named imports
  for (const { imported, local } of parsed.namedImports) {
    const solidName = HOOK_RENAME[imported];
    if (solidName === undefined) {
      // Unknown React export — keep as solid-js import with same name
      solidImports.push(imported === local ? imported : `${imported} as ${local}`);
    } else if (solidName !== null) {
      // Renamed
      const solidLocal = local === imported ? solidName : local;
      solidImports.push(solidName === solidLocal ? solidName : `${solidName} as ${solidLocal}`);
    }
    // null → drop (useCallback, useRef, etc.)
  }

  // Handle `import React from "react"` or `import React, { ... } from "react"`
  // React namespace is handled via `React.X` replacements below

  if (solidImports.length === 0) {
    // Nothing to import from solid-js (all were dropped or only React default)
    return "";
  }

  // Deduplicate
  const unique = [...new Set(solidImports)];
  return `import { ${unique.join(", ")} } from "solid-js";`;
}

/**
 * Transform an @opentui/react import.
 * Remaps named imports and changes the source to @opentui/solid.
 */
function transformOpentuiReactImport(line: string): string {
  const parsed = parseImportLine(line);
  if (!parsed) return line.replace(/@opentui\/react(\/[^"']*)?/g, "@opentui/solid");

  const solidImports: string[] = [];
  for (const { imported, local } of parsed.namedImports) {
    const solidName = OPENTUI_RENAME[imported] ?? imported;
    solidImports.push(solidName === local ? solidName : `${solidName} as ${local}`);
  }

  if (solidImports.length === 0) return "";

  const unique = [...new Set(solidImports)];
  return `import { ${unique.join(", ")} } from "@opentui/solid";`;
}

/**
 * Remove deps array from useEffect/useMemo/createEffect/createMemo calls.
 * Handles simple single-line cases: }, [...]); or }, []);
 * Multi-line deps left alone (TypeScript won't error since SolidJS accepts extra args).
 */
function removeDepsArrays(content: string): string {
  // Pattern: closing brace/paren of callback, then `, [anything]` before final `);` or `);`
  // e.g. `}, [a, b]);` → `});`
  // e.g. `}, []);` → `});`
  // Only on lines that contain the pattern (not multi-line arrays)
  return content.replace(/(\})\s*,\s*\[[^\]]*\]\s*(\))/g, "$1$2");
}

// ─────────────────────────────────────────────
// Main file transformer
// ─────────────────────────────────────────────

function migrateFile(filePath: string): { changed: boolean; content: string } {
  let content = readFileSync(filePath, "utf8");
  const original = content;

  const lines = content.split("\n");
  const outLines: string[] = [];

  let solidImportsToAdd = new Set<string>();

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]!;
    const trimmed = line.trim();

    // ── React imports ─────────────────────────────────────────────────────
    if (/^import\s+(type\s+)?(React|\w+)\s*(,\s*\{[^}]*\})?\s*from\s*["']react["']/.test(trimmed)
      || /^import\s+\{[^}]*\}\s*from\s*["']react["']/.test(trimmed)
      || /^import\s+type\s+\{[^}]*\}\s*from\s*["']react["']/.test(trimmed)) {
      // Handle multi-line imports (collect until closing)
      let fullImport = line;
      let j = i;
      while (!fullImport.includes('from "react"') && !fullImport.includes("from 'react'") && j < lines.length - 1) {
        j++;
        fullImport += "\n" + lines[j]!;
        i = j;
      }

      // type-only react imports (e.g. import type { ReactNode } from "react") → drop
      if (/^import\s+type/.test(fullImport.trim())) {
        outLines.push(""); // remove type-only react import
        continue;
      }

      const transformed = transformReactImport(fullImport.replace(/\n/g, " ").trim());
      outLines.push(transformed);
      continue;
    }

    // ── @opentui/react imports ─────────────────────────────────────────────
    if (/from\s*["']@opentui\/react(\/[^"']*)?["']/.test(trimmed)) {
      let fullImport = line;
      let j = i;
      while (
        !fullImport.includes('from "@opentui/react') &&
        !fullImport.includes("from '@opentui/react") &&
        j < lines.length - 1
      ) {
        j++;
        fullImport += "\n" + lines[j]!;
        i = j;
      }
      const transformed = transformOpentuiReactImport(
        fullImport.replace(/\n/g, " ").trim(),
      );
      outLines.push(transformed);
      continue;
    }

    outLines.push(line);
  }

  content = outLines.join("\n");

  // ── React.ReactNode → JSX.Element ─────────────────────────────────────
  content = content.replace(/:\s*React\.ReactNode\b/g, ": JSX.Element");
  content = content.replace(/\bReact\.ReactNode\b/g, "JSX.Element");
  content = content.replace(/:\s*ReactNode\b/g, ": JSX.Element");

  // ── React.lazy → lazy ─────────────────────────────────────────────────
  content = content.replace(/\bReact\.lazy\(/g, "lazy(");

  // ── React.memo → (just remove wrapper, keep component) ────────────────
  // React.memo(Component) → Component  (simple case only)
  content = content.replace(/\bReact\.memo\((\w+)\)/g, "$1");

  // ── React.Fragment ─────────────────────────────────────────────────────
  content = content.replace(/\bReact\.Fragment\b/g, "Fragment");
  content = content.replace(/\bReact\.createElement\b/g, "/* React.createElement - manual fix needed */");

  // ── Hook renames ───────────────────────────────────────────────────────
  // Handles both plain and generic calls: useState( and useState<T>(
  const GENERIC = "(?:<[^>]*>)?\\s*";
  content = content.replace(new RegExp(`\\buseState${GENERIC}\\(`, "g"), (m) =>
    m.replace("useState", "createSignal"));
  content = content.replace(new RegExp(`\\buseEffect${GENERIC}\\(`, "g"), (m) =>
    m.replace("useEffect", "createEffect"));
  content = content.replace(new RegExp(`\\buseLayoutEffect${GENERIC}\\(`, "g"), (m) =>
    m.replace("useLayoutEffect", "createEffect"));
  content = content.replace(new RegExp(`\\buseMemo${GENERIC}\\(`, "g"), (m) =>
    m.replace("useMemo", "createMemo"));
  // Note: useCallback and useRef NOT renamed — TS errors guide manual unwrapping

  // ── React type remaps ─────────────────────────────────────────────────
  // Dispatch<SetStateAction<T>> → Setter<T>
  content = content.replace(/\bReact\.Dispatch\s*<\s*React\.SetStateAction\s*<([^>]+)>\s*>/g, "Setter<$1>");
  content = content.replace(/\bDispatch\s*<\s*SetStateAction\s*<([^>]+)>\s*>/g, "Setter<$1>");
  content = content.replace(/\bReact\.SetStateAction\s*<([^>]+)>/g, "$1 | ((prev: $1) => $1)");
  // MutableRefObject<T> → not needed (useRef removed), replace with T
  content = content.replace(/\bReact\.MutableRefObject\s*<([^>]+)>/g, "$1");
  content = content.replace(/\bMutableRefObject\s*<([^>]+)>/g, "$1");

  // ── Remove simple deps arrays ──────────────────────────────────────────
  content = removeDepsArrays(content);

  // ── Ensure JSX type is imported if JSX.Element is used ─────────────────
  const hasJsxElement = /\bJSX\.Element\b/.test(content);
  const hasJsxTypeImport = /import\s+type\s+\{\s*JSX/.test(content) || /import\s+\{[^}]*\bJSX\b/.test(content);

  if (hasJsxElement && !hasJsxTypeImport) {
    const solidImportMatch = content.match(/^(import\s+\{[^}]+\}\s*from\s*["']solid-js["'];?)/m);
    if (solidImportMatch) {
      const existing = solidImportMatch[1]!;
      const insertAfter = content.indexOf(existing) + existing.length;
      content = content.slice(0, insertAfter) + '\nimport type { JSX } from "solid-js";' + content.slice(insertAfter);
    } else {
      content = 'import type { JSX } from "solid-js";\n' + content;
    }
  }

  // ── Ensure Setter is imported from solid-js if used ───────────────────
  const hasSetter = /\bSetter\s*</.test(content);
  const hasSetterImport = /import\s+\{[^}]*\bSetter\b/.test(content);
  if (hasSetter && !hasSetterImport) {
    // Add Setter to existing solid-js import
    content = content.replace(
      /^(import\s+\{)([^}]+)(\}\s*from\s*["']solid-js["'])/m,
      (_, open, names, close) => `${open}${names.trimEnd()}, Setter ${close}`,
    );
  }

  // ── Clean up blank lines left by removed imports ───────────────────────
  content = content.replace(/\n{3,}/g, "\n\n");

  return { changed: content !== original, content };
}

// ─────────────────────────────────────────────
// Run
// ─────────────────────────────────────────────

const args = process.argv.slice(2);
const dryRun = args.includes("--dry-run");
const verbose = args.includes("--verbose");

let changed = 0;
let unchanged = 0;
let errors = 0;

for (const dir of DIRS) {
  let files: string[];
  try { files = getAllFiles(dir); } catch { continue; }

  for (const file of files) {
    try {
      const result = migrateFile(file);
      if (result.changed) {
        changed++;
        const rel = relative(ROOT, file);
        console.log(`  ✓  ${rel}`);
        if (!dryRun) {
          writeFileSync(file, result.content, "utf8");
        }
      } else {
        unchanged++;
        if (verbose) {
          console.log(`  -  ${relative(ROOT, file)}`);
        }
      }
    } catch (err) {
      errors++;
      console.error(`  ✗  ${relative(ROOT, file)}: ${err}`);
    }
  }
}

console.log(`\nDone. Changed: ${changed}  Unchanged: ${unchanged}  Errors: ${errors}`);
if (dryRun) console.log("(dry run — no files written)");
