import type { JSX } from "solid-js";
/**
 * Agent status detail view: phase badge, conditions, resource usage, identity.
 *
 * All content is rendered unconditionally with ternary expressions in each
 * <text> node. Avoids <Show>/&& patterns which evaluate once inside <Match>
 * branches and don't re-render when async data arrives.
 */

import type { AgentStatus, AgentSpec, AgentIdentity, AgentPhase } from "../../stores/agents-store.js";

interface AgentStatusViewProps {
  readonly status: AgentStatus | null;
  readonly spec: AgentSpec | null;
  readonly identity: AgentIdentity | null;
  readonly loading: boolean;
  readonly trustScore?: number | null;
  readonly reputation?: unknown | null;
}

const PHASE_BADGES: Readonly<Record<AgentPhase, string>> = {
  warming: "[WRM]", ready: "[RDY]", active: "[ACT]", thinking: "[THK]",
  idle: "[IDL]", suspended: "[SUS]", evicted: "[EVT]",
};

function fmt(ts: string | null): string {
  if (!ts) return "n/a";
  try { return new Date(ts).toLocaleString(); } catch { return ts; }
}

function bar(pct: number, w: number): string {
  const f = Math.round((pct / 100) * w);
  return `[${"#".repeat(f)}${"-".repeat(w - f)}] ${pct.toFixed(0)}%`;
}

function hex(s: string | null | undefined, n = 16): string {
  if (!s) return "n/a";
  return s.length > n ? s.slice(0, n) + "..." : s;
}

export function AgentStatusView(props: AgentStatusViewProps): JSX.Element {
  // Read props reactively inside each <text> expression — no <Show> or && needed.
  const s = () => props.status;
  const badge = () => { const st = s(); return st ? (PHASE_BADGES[st.phase] ?? `[${st.phase.toUpperCase()}]`) : ""; };

  return (
    <scrollbox height="100%" width="100%">
      <text>{props.loading ? "⠋ Loading agent status..." : !s() ? "Select an agent to view status" : `Phase: ${badge()} ${s()!.phase}  |  Gen: ${s()!.observed_generation}`}</text>
      <text>{s() ? `Last heartbeat: ${fmt(s()!.last_heartbeat)}` : ""}</text>
      <text>{s() ? `Last activity:  ${fmt(s()!.last_activity)}` : ""}</text>
      <text>{s() ? `Inbox: ${s()!.inbox_depth}  |  Context: ${s()!.context_usage_pct}%` : ""}</text>
      <text>{s() ? "--- Resources ---" : ""}</text>
      <text>{s() ? `Tokens:  ${s()!.resource_usage.tokens_used}` : ""}</text>
      <text>{s() ? `Storage: ${s()!.resource_usage.storage_used_mb} MB` : ""}</text>
      <text>{s() ? `Context: ${bar(s()!.resource_usage.context_usage_pct, 20)}` : ""}</text>
      <text>{props.spec ? `--- Spec: ${props.spec.agent_type} | QoS: ${props.spec.qos_class} ---` : ""}</text>
      <text>{props.identity ? `DID: ${props.identity.did}` : ""}</text>
      <text>{props.identity ? `Key: ${hex(props.identity.public_key_hex)}` : ""}</text>
      <text>{props.trustScore != null ? `Trust: ${props.trustScore.toFixed(2)}` : ""}</text>
    </scrollbox>
  );
}
