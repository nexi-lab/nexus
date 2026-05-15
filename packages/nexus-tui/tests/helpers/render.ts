import { testRender as solidTestRender } from "@opentui/solid";

if (!("Element" in globalThis)) {
  Object.defineProperty(globalThis, "Element", {
    value: class Element {},
    configurable: true,
    writable: true,
  });
}

export const testRender = solidTestRender;
