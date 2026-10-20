import { useGlobalStore } from "../../stores/global-store.js";
export function StatusBar() {
  const state = useGlobalStore.getState;
  const principal = () => state().userInfo;
  return <box height={1} width="100%"><text>
    {`${state().connectionStatus} | ${state().config.baseUrl ?? ""} | v${state().serverVersion ?? "?"}`}
    {principal() ? ` | ${principal()!.subject_type}:${principal()!.subject_id} | zone:${principal()!.zone_id}` : " | identity unavailable"}
  </text></box>;
}
