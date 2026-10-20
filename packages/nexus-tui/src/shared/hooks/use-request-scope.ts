import { createSignal, onCleanup } from "solid-js";

export function useRequestScope() {
  const controller = new AbortController();
  const [pending, setPending] = createSignal(false);
  const [error, setError] = createSignal<string | null>(null);
  let requestId = 0;
  onCleanup(() => controller.abort());
  return {
    signal: controller.signal, pending, error,
    async run<T>(request: (signal: AbortSignal) => Promise<T>, accept: (value: T) => void) {
      if (pending() || controller.signal.aborted) return;
      const id = ++requestId;
      setPending(true); setError(null);
      try {
        const value = await request(controller.signal);
        if (!controller.signal.aborted) { setPending(false); accept(value); }
      } catch (error) {
        if (!controller.signal.aborted && id === requestId) setError(error instanceof Error ? error.message : String(error));
      } finally { if (!controller.signal.aborted && id === requestId) setPending(false); }
    },
  };
}
