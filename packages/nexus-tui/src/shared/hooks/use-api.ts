/**
 * Hook to get the configured FetchClient from the global store.
 */

import { useGlobalStore } from "../../stores/global-store.js";
import type { FetchClient } from "@nexus-ai-fs/api-client";

export function useApi(): FetchClient | null {
  return useGlobalStore((state) => state.client);
}
