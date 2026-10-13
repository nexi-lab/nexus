import { expect, it } from "bun:test";
import { createWorkerManager } from "../../src/worker/worker-manager.js";

it("keeps pending reads separate after a worker credential change", async () => {
  let firstSeen!: () => void;
  let releaseFirst!: () => void;
  const received = new Promise<void>(resolve => { firstSeen = resolve; });
  const held = new Promise<void>(resolve => { releaseFirst = resolve; });
  const credentials: (string | null)[] = [];
  const server = Bun.serve({
    hostname: "127.0.0.1",
    port: 0,
    async fetch(request) {
      const credential = request.headers.get("authorization");
      credentials.push(credential);
      if (credentials.length === 1) {
        firstSeen();
        await held;
      }
      return Response.json({ credential });
    },
  });
  const config = { baseUrl: `http://127.0.0.1:${server.port}`, maxRetries: 0 };
  const manager = createWorkerManager({ ...config, apiKey: "first-caller" });
  const options = { timeout: 5000 };
  const first = manager.client.get<{ credential: string }>("/probe", options);
  try {
    await Promise.race([received, first]);
    manager.reconfigure({ ...config, apiKey: "second-caller" });
    const second = await manager.client.get<{ credential: string }>("/probe", options);
    expect(second.credential).toBe("Bearer second-caller");
    expect(credentials).toEqual(["Bearer first-caller", "Bearer second-caller"]);
    releaseFirst();
    expect((await first).credential).toBe("Bearer first-caller");
  } finally {
    releaseFirst();
    await first.catch(() => {});
    manager.terminate();
    server.stop(true);
  }
}, 15000);
