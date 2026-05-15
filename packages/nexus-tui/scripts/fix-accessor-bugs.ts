#!/usr/bin/env bun
/**
 * Fix specific bugs introduced by fix-signal-calls.ts:
 *
 * 1. `getIndex(): () => {`  →  `getIndex: () => {`  (broken object method)
 * 2. `name?().property`     →  `name()?.property`   (broken optional chain)
 * 3. `prop()={value}`       →  `prop={value()}`     (broken JSX prop name)
 * 4. `name(),` in object    →  `name: name(),`      (broken object shorthand)
 */

import { readFileSync, writeFileSync } from "fs";

const ROOT = new URL("../", import.meta.url).pathname;

function fix(relPath: string, transforms: Array<[RegExp | string, string]>): void {
  const path = ROOT + relPath;
  let content: string;
  try { content = readFileSync(path, "utf8"); } catch (e) { console.error(`  ✗ ${relPath}: ${e}`); return; }
  const original = content;

  for (const [pattern, replacement] of transforms) {
    if (typeof pattern === "string") {
      content = content.replaceAll(pattern, replacement);
    } else {
      content = content.replace(pattern, replacement);
    }
  }

  if (content !== original) {
    writeFileSync(path, content, "utf8");
    console.log(`  ✓ ${relPath}`);
  } else {
    console.log(`  - ${relPath} (no change)`);
  }
}

// ─── Fix 1: broken object method syntax ───────────────────────────────────────
// `getIndex(): () => {`  →  `getIndex: () => {`
// This happened because the script added () after `getIndex` treating it as a signal

fix("src/panels/access/access-panel.tsx", [
  ["    getIndex(): () => {", "    getIndex: () => {"],
]);

fix("src/panels/payments/payments-panel.tsx", [
  ["    getIndex(): () => {", "    getIndex: () => {"],
]);

// ─── Fix 2: broken optional chain ─────────────────────────────────────────────
// `expandedDelegation?().delegation_id`  →  `expandedDelegation()?.delegation_id`

fix("src/panels/agents/agents-panel.tsx", [
  // Optional chain broken: name?().prop  →  name()?.prop
  [/(\w+)\?\(\)\.(\w+)/g, "$1()?.$$2"],
  // JSX prop name broken: message()={val}  →  message={val()}
  ["message()={operationLoading}", "message={operationLoading()}"],
]);

// ─── Fix 3: broken JSX prop names ─────────────────────────────────────────────
// `path()={editorPath}`  →  `path={editorPath()}`
// `selectedIndex()={...}`  →  `selectedIndex={...}` (with signal fixed in value)

fix("src/panels/files/file-explorer-panel.tsx", [
  ["path()={editorPath}", "path={editorPath()}"],
]);

// events-tab.tsx and events-panel.tsx: selectedIndex()={...}
// The value also needs selectedEventIndex() → selectedEventIndex() for the else branch
const eventsSelectedIndexBefore =
  "selectedIndex()={selectedEventIndex() >= 0 ? selectedEventIndex : events.length - 1}";
const eventsSelectedIndexAfter =
  "selectedIndex={selectedEventIndex() >= 0 ? selectedEventIndex() : events.length - 1}";

fix("src/panels/events/events-tab.tsx", [
  [eventsSelectedIndexBefore, eventsSelectedIndexAfter],
]);

fix("src/panels/events/events-panel.tsx", [
  [eventsSelectedIndexBefore, eventsSelectedIndexAfter],
]);

// ─── Fix 4: broken object shorthands in events-panel bindingCtx ────────────────
// Signal accessors used as object shorthand keys, e.g. `filterMode()` in object
// These need to become `filterMode: filterMode()`

const eventsObjectFixes: Array<[string, string]> = [
  // filterMode(), filterBuffer(), → filterMode: filterMode(), filterBuffer: filterBuffer(),
  ["    filterMode(), filterBuffer(), setFilterMode, setFilterBuffer,",
   "    filterMode: filterMode(), filterBuffer: filterBuffer(), setFilterMode, setFilterBuffer,"],
  ["    events, selectedEventIndex(), setSelectedEventIndex,",
   "    events, selectedEventIndex: selectedEventIndex(), setSelectedEventIndex,"],
  ["    expandedEventIndex(), setExpandedEventIndex,",
   "    expandedEventIndex: expandedEventIndex(), setExpandedEventIndex,"],
  ["    mclUrnFilter(), setMclUrnFilter, mclAspectFilter(), setMclAspectFilter,",
   "    mclUrnFilter: mclUrnFilter(), setMclUrnFilter, mclAspectFilter: mclAspectFilter(), setMclAspectFilter,"],
  ["    replayTypeFilter(), setReplayTypeFilter, clearEventReplay, fetchEventReplay,",
   "    replayTypeFilter: replayTypeFilter(), setReplayTypeFilter, clearEventReplay, fetchEventReplay,"],
  ["    connectorDetailView(), setConnectorDetailView,",
   "    connectorDetailView: connectorDetailView(), setConnectorDetailView,"],
  ["    secretsFilter(), setSecretsFilter, fetchSecretAudit,",
   "    secretsFilter: secretsFilter(), setSecretsFilter, fetchSecretAudit,"],
  ["    auditTransactions, selectedAuditIndex(), setSelectedAuditIndex,",
   "    auditTransactions, selectedAuditIndex: selectedAuditIndex(), setSelectedAuditIndex,"],
  // Also fix the inverse prop in .map callback — index is a number parameter, not a signal
  ["inverse={index() === selectedEventIndex", "inverse={index === selectedEventIndex"],
];

fix("src/panels/events/events-panel.tsx", eventsObjectFixes);
fix("src/panels/events/events-tab.tsx", eventsObjectFixes);

// ─── Also fix agents-panel optional chain ─────────────────────────────────────
// The $$ replacement above won't work in template literals, fix with direct string replace
// (The regex $1()?.$$2 used $$ which is the JS regex replacement for literal $)
// Re-fix: use a direct string replace
fix("src/panels/agents/agents-panel.tsx", [
  ["expandedDelegation?().delegation_id", "expandedDelegation()?.delegation_id"],
]);

console.log("\nDone.");
