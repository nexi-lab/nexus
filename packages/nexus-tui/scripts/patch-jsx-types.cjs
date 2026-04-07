#!/usr/bin/env node
/**
 * Patches @opentui/solid jsx-runtime.d.ts to add missing function exports.
 * The package only declares JSX namespace types but not the jsx/jsxs/jsxDEV
 * factory functions, causing bun to fail when resolving .d.ts before .js.
 */
const fs = require("fs");
const path = require("path");

const dtsPath = path.join(
  __dirname, "..", "node_modules", "@opentui", "solid", "jsx-runtime.d.ts"
);

if (!fs.existsSync(dtsPath)) process.exit(0);

const content = fs.readFileSync(dtsPath, "utf8");
if (content.includes("export function jsx")) process.exit(0); // already patched

const patch = [
  "export function jsx(type: any, props: any, key?: any): any;",
  "export function jsxs(type: any, props: any, key?: any): any;",
  "export function jsxDEV(type: any, props: any, key?: any): any;",
  "export function Fragment(props: { children?: any }): any;",
  "",
].join("\n");

fs.writeFileSync(dtsPath, patch + content);
console.log("Patched @opentui/solid jsx-runtime.d.ts");
