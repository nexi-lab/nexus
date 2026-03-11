/**
 * Payments panel: tabbed layout for Balance, Reservations, Policies, and Audit views.
 */

import React, { useEffect } from "react";
import { usePaymentsStore } from "../../stores/payments-store.js";
import type { PaymentsTab } from "../../stores/payments-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { BalanceCard } from "./balance-card.js";
import { ReservationList } from "./reservation-list.js";
import { PolicyList } from "./policy-list.js";
import { AuditLog } from "./audit-log.js";

const TAB_ORDER: readonly PaymentsTab[] = [
  "balance",
  "reservations",
  "policies",
  "audit",
];
const TAB_LABELS: Readonly<Record<PaymentsTab, string>> = {
  balance: "Balance",
  reservations: "Reservations",
  policies: "Policies",
  audit: "Audit",
};

export default function PaymentsPanel(): React.ReactNode {
  const client = useApi();

  const balance = usePaymentsStore((s) => s.balance);
  const balanceLoading = usePaymentsStore((s) => s.balanceLoading);
  const reservations = usePaymentsStore((s) => s.reservations);
  const selectedReservationIndex = usePaymentsStore((s) => s.selectedReservationIndex);
  const reservationsLoading = usePaymentsStore((s) => s.reservationsLoading);
  const policies = usePaymentsStore((s) => s.policies);
  const policiesLoading = usePaymentsStore((s) => s.policiesLoading);
  const auditEntries = usePaymentsStore((s) => s.auditEntries);
  const auditTotal = usePaymentsStore((s) => s.auditTotal);
  const auditLoading = usePaymentsStore((s) => s.auditLoading);
  const activeTab = usePaymentsStore((s) => s.activeTab);
  const error = usePaymentsStore((s) => s.error);

  const fetchBalance = usePaymentsStore((s) => s.fetchBalance);
  const fetchReservations = usePaymentsStore((s) => s.fetchReservations);
  const fetchPolicies = usePaymentsStore((s) => s.fetchPolicies);
  const fetchAudit = usePaymentsStore((s) => s.fetchAudit);
  const commitReservation = usePaymentsStore((s) => s.commitReservation);
  const releaseReservation = usePaymentsStore((s) => s.releaseReservation);
  const setActiveTab = usePaymentsStore((s) => s.setActiveTab);
  const setSelectedReservationIndex = usePaymentsStore(
    (s) => s.setSelectedReservationIndex,
  );

  // Refresh current view based on active tab
  const refreshCurrentView = (): void => {
    if (!client) return;

    if (activeTab === "balance") {
      fetchBalance(client);
    } else if (activeTab === "reservations") {
      fetchReservations(client);
    } else if (activeTab === "policies") {
      fetchPolicies(client);
    } else if (activeTab === "audit") {
      fetchAudit(client);
    }
  };

  // Auto-fetch when tab changes
  useEffect(() => {
    refreshCurrentView();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, client]);

  useKeyboard({
    j: () => {
      if (activeTab === "reservations") {
        setSelectedReservationIndex(
          Math.min(selectedReservationIndex + 1, reservations.length - 1),
        );
      }
    },
    down: () => {
      if (activeTab === "reservations") {
        setSelectedReservationIndex(
          Math.min(selectedReservationIndex + 1, reservations.length - 1),
        );
      }
    },
    k: () => {
      if (activeTab === "reservations") {
        setSelectedReservationIndex(Math.max(selectedReservationIndex - 1, 0));
      }
    },
    up: () => {
      if (activeTab === "reservations") {
        setSelectedReservationIndex(Math.max(selectedReservationIndex - 1, 0));
      }
    },
    tab: () => {
      const currentIdx = TAB_ORDER.indexOf(activeTab);
      const nextIdx = (currentIdx + 1) % TAB_ORDER.length;
      const nextTab = TAB_ORDER[nextIdx];
      if (nextTab) {
        setActiveTab(nextTab);
      }
    },
    r: () => refreshCurrentView(),
    c: () => {
      if (activeTab !== "reservations" || !client) return;
      const selected = reservations[selectedReservationIndex];
      if (selected && selected.status === "active") {
        commitReservation(selected.reservation_id, client);
      }
    },
    x: () => {
      if (activeTab !== "reservations" || !client) return;
      const selected = reservations[selectedReservationIndex];
      if (selected && selected.status === "active") {
        releaseReservation(selected.reservation_id, client);
      }
    },
  });

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Tab bar */}
      <box height={1} width="100%">
        <text>
          {TAB_ORDER.map((tab) => {
            const label = TAB_LABELS[tab];
            return tab === activeTab ? `[${label}]` : ` ${label} `;
          }).join(" ")}
        </text>
      </box>

      {/* Error display */}
      {error && (
        <box height={1} width="100%">
          <text>{`Error: ${error}`}</text>
        </box>
      )}

      {/* Detail content */}
      <box flexGrow={1} borderStyle="single">
        {activeTab === "balance" && (
          <BalanceCard balance={balance} loading={balanceLoading} />
        )}
        {activeTab === "reservations" && (
          <ReservationList
            reservations={reservations}
            selectedIndex={selectedReservationIndex}
            loading={reservationsLoading}
          />
        )}
        {activeTab === "policies" && (
          <PolicyList policies={policies} loading={policiesLoading} />
        )}
        {activeTab === "audit" && (
          <AuditLog
            entries={auditEntries}
            total={auditTotal}
            loading={auditLoading}
          />
        )}
      </box>

      {/* Help bar */}
      <box height={1} width="100%">
        <text>
          {"j/k:navigate  Tab:switch tab  r:refresh  c:commit reservation  x:release reservation  q:quit"}
        </text>
      </box>
    </box>
  );
}
