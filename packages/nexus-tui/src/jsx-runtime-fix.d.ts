/**
 * Augment @opentui/solid jsx-runtime with missing function exports.
 * The package's jsx-runtime.d.ts only declares JSX namespace types
 * but not the jsx/jsxs/jsxDEV factory functions that bun expects.
 */
declare module "@opentui/solid/jsx-runtime" {
  export function jsx(type: any, props: any, key?: any): any;
  export function jsxs(type: any, props: any, key?: any): any;
  export function Fragment(props: { children?: any }): any;
}

declare module "@opentui/solid/jsx-dev-runtime" {
  export function jsxDEV(type: any, props: any, key?: any): any;
  export function Fragment(props: { children?: any }): any;
}
