const fs = require("fs");
const path = require("path");
const dtsPath = path.join(__dirname, "..", "node_modules", "@opentui", "solid", "jsx-runtime.d.ts");

if (fs.existsSync(dtsPath)) {
  const content = fs.readFileSync(dtsPath, "utf8");
  if (!content.includes("export function jsx")) {
    const patch = [
      "export function jsx(type: any, props: any, key?: any): any;",
      "export function jsxs(type: any, props: any, key?: any): any;",
      "export function jsxDEV(type: any, props: any, key?: any): any;",
      "export function Fragment(props: { children?: any }): any;",
      "",
    ].join("\n");
    fs.writeFileSync(dtsPath, patch + content);
  }
}

const elementTypesPath = path.join(
  __dirname,
  "..",
  "node_modules",
  "@opentui",
  "solid",
  "src",
  "types",
  "elements.d.ts",
);

if (fs.existsSync(elementTypesPath)) {
  let content = fs.readFileSync(elementTypesPath, "utf8");
  content = content.replace(
    "export type ElementProps<TRenderable = unknown> = {\n    ref?: Ref<TRenderable>;\n};",
    "export type ElementProps<TRenderable = unknown> = {\n    key?: string | number;\n    ref?: Ref<TRenderable>;\n};",
  );
  content = content.replace(
    "export type SpanProps = ComponentProps<{}, TextNodeRenderable> & {",
    "export type SpanProps = ComponentProps<TextNodeOptions, TextNodeRenderable> & {",
  );
  fs.writeFileSync(elementTypesPath, content);
}
