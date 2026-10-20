import { testRender as solidTestRender } from "@opentui/solid";

if (!("Element" in globalThis)) {
  Object.defineProperty(globalThis, "Element", {
    value: class Element {},
    configurable: true,
    writable: true,
  });
}

export const testRender = solidTestRender;

export async function waitForFrame(setup: Awaited<ReturnType<typeof testRender>>, text: string, timeout = 3000): Promise<string> {
  const deadline = performance.now() + timeout;
  do {
    await setup.renderOnce();
    const frame = setup.captureCharFrame();
    if (frame.includes(text)) return frame;
    await Bun.sleep(10);
  } while (performance.now() < deadline);
  throw new Error(`Missing ${text}:\n${setup.captureCharFrame()}`);
}
