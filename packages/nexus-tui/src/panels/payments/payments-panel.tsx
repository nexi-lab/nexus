/**
 * Payments panel: tabbed layout for Balance, Reservations, Transactions,
 * and Policies views.
 */

import React, { useState, useCallback, useEffect } from "react";
import { usePaymentsStore } from "../../stores/payments-store.js";
import type { PaymentsTab } from "../../stores/payments-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useConfirmStore } from "../../shared/hooks/use-confirm.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { BrickGate } from "../../shared/components/brick-gate.js";
import { BalanceCard } from "./balance-card.js";
import { ReservationList } from "./reservation-list.js";
import { TransferForm } from "./transfer-form.js";
import { TransactionList } from "./transaction-list.js";
import { PolicyList } from "./policy-list.js";
import { BudgetCard } from "./budget-card.js";

const TAB_ORDER: readonly PaymentsTab[] = [
  "balance",
  "reservations",
  "transactions",
  "policies",
];
const TAB_LABELS: Readonly<Record<PaymentsTab, string>> = {
  balance: "Balance",
  reservations: "Reservations",
  transactions: "Transactions",
  policies: "Policies",
};

export default function PaymentsPanel(): React.ReactNode {
  const client = useApi();
  const confirm = useConfirmStore((s) => s.confirm);
  const [showTransfer, setShowTransfer] = useState(false);
  const [affordInputMode, setAffordInputMode] = useState(false);
  const [affordBuffer, setAffordBuffer] = useState("");
  const [policyInputMode, setPolicyInputMode] = useState(false);
  const [policyBuffer, setPolicyBuffer] = useState("");

  const balance = usePaymentsStore((s) => s.balance);
  const balanceLoading = usePaymentsStore((s) => s.balanceLoading);
  const reservations = usePaymentsStore((s) => s.reservations);
  const selectedReservationIndex = usePaymentsStore((s) => s.selectedReservationIndex);
  const reservationsLoading = usePaymentsStore((s) => s.reservationsLoading);
  const transactions = usePaymentsStore((s) => s.transactions);
  const transactionsLoading = usePaymentsStore((s) => s.transactionsLoading);
  const selectedTransactionIndex = usePaymentsStore((s) => s.selectedTransactionIndex);
  const policies = usePaymentsStore((s) => s.policies);
  const policiesLoading = usePaymentsStore((s) => s.policiesLoading);
  const budget = usePaymentsStore((s) => s.budget);
  const budgetLoading = usePaymentsStore((s) => s.budgetLoading);
  const activeTab = usePaymentsStore((s) => s.activeTab);
  const error = usePaymentsStore((s) => s.error);

  const fetchBalance = usePaymentsStore((s) => s.fetchBalance);
  const transfer = usePaymentsStore((s) => s.transfer);
  const commitReservation = usePaymentsStore((s) => s.commitReservation);
  const releaseReservation = usePaymentsStore((s) => s.releaseReservation);
  const transactionsHasMore = usePaymentsStore((s) => s.transactionsHasMore);
  const transactionsCursorStack = usePaymentsStore((s) => s.transactionsCursorStack);
  const integrityResult = usePaymentsStore((s) => s.integrityResult);
  const fetchTransactions = usePaymentsStore((s) => s.fetchTransactions);
  const fetchNextTransactions = usePaymentsStore((s) => s.fetchNextTransactions);
  const fetchPrevTransactions = usePaymentsStore((s) => s.fetchPrevTransactions);
  const verifyIntegrity = usePaymentsStore((s) => s.verifyIntegrity);
  const fetchPolicies = usePaymentsStore((s) => s.fetchPolicies);
  const fetchBudget = usePaymentsStore((s) => s.fetchBudget);
  const deletePolicy = usePaymentsStore((s) => s.deletePolicy);
  const checkAfford = usePaymentsStore((s) => s.checkAfford);
  const affordResult = usePaymentsStore((s) => s.affordResult);
  const createPolicy = usePaymentsStore((s) => s.createPolicy);
  const setActiveTab = usePaymentsStore((s) => s.setActiveTab);
  const setSelectedReservationIndex = usePaymentsStore(
    (s) => s.setSelectedReservationIndex,
  );
  const setSelectedTransactionIndex = usePaymentsStore(
    (s) => s.setSelectedTransactionIndex,
  );
  const [selectedPolicyIndex, setSelectedPolicyIndex] = useState(0);

  const handleTransferSubmit = useCallback(
    async (to: string, amount: string, memo: string) => {
      if (!client) return;
      const ok = await confirm("Transfer funds?", `Transfer ${amount} credits to ${to}. This cannot be undone.`);
      if (!ok) return;
      transfer(to, amount, memo, client);
      setShowTransfer(false);
    },
    [client, transfer, confirm],
  );

  const handleTransferCancel = useCallback(() => {
    setShowTransfer(false);
  }, []);

  // Refresh current view based on active tab.
  // Reservations are tracked locally, so no fetch is needed for that tab.
  const refreshCurrentView = (): void => {
    if (!client) return;

    if (activeTab === "balance") {
      fetchBalance(client);
    } else if (activeTab === "transactions") {
      fetchTransactions(client);
    } else if (activeTab === "policies") {
      fetchPolicies(client);
    }
  };

  // Auto-fetch when tab changes
  useEffect(() => {
    refreshCurrentView();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, client]);

  const handleAffordUnhandled = useCallback(
    (keyName: string) => {
      if (!affordInputMode) return;
      if (keyName.length === 1 && /[\d.]/.test(keyName)) {
        setAffordBuffer((b) => b + keyName);
      }
    },
    [affordInputMode],
  );

  useKeyboard(
    showTransfer
      ? {}
      : affordInputMode
        ? {
            return: () => {
              const amount = affordBuffer.trim();
              if (amount && client) checkAfford(amount, client);
              setAffordInputMode(false);
              setAffordBuffer("");
            },
            escape: () => { setAffordInputMode(false); setAffordBuffer(""); },
            backspace: () => { setAffordBuffer((b) => b.slice(0, -1)); },
          }
        : policyInputMode
          ? {
              return: () => {
                const name = policyBuffer.trim();
                if (name && client) createPolicy(name, {}, client);
                setPolicyInputMode(false);
                setPolicyBuffer("");
              },
              escape: () => { setPolicyInputMode(false); setPolicyBuffer(""); },
              backspace: () => { setPolicyBuffer((b) => b.slice(0, -1)); },
            }
          : {
          j: () => {
            if (activeTab === "reservations") {
              setSelectedReservationIndex(
                Math.min(selectedReservationIndex + 1, reservations.length - 1),
              );
            } else if (activeTab === "transactions") {
              setSelectedTransactionIndex(
                Math.min(selectedTransactionIndex + 1, transactions.length - 1),
              );
            } else if (activeTab === "policies") {
              setSelectedPolicyIndex(
                Math.min(selectedPolicyIndex + 1, policies.length - 1),
              );
            }
          },
          down: () => {
            if (activeTab === "reservations") {
              setSelectedReservationIndex(
                Math.min(selectedReservationIndex + 1, reservations.length - 1),
              );
            } else if (activeTab === "transactions") {
              setSelectedTransactionIndex(
                Math.min(selectedTransactionIndex + 1, transactions.length - 1),
              );
            } else if (activeTab === "policies") {
              setSelectedPolicyIndex(
                Math.min(selectedPolicyIndex + 1, policies.length - 1),
              );
            }
          },
          k: () => {
            if (activeTab === "reservations") {
              setSelectedReservationIndex(Math.max(selectedReservationIndex - 1, 0));
            } else if (activeTab === "transactions") {
              setSelectedTransactionIndex(Math.max(selectedTransactionIndex - 1, 0));
            } else if (activeTab === "policies") {
              setSelectedPolicyIndex(Math.max(selectedPolicyIndex - 1, 0));
            }
          },
          up: () => {
            if (activeTab === "reservations") {
              setSelectedReservationIndex(Math.max(selectedReservationIndex - 1, 0));
            } else if (activeTab === "transactions") {
              setSelectedTransactionIndex(Math.max(selectedTransactionIndex - 1, 0));
            } else if (activeTab === "policies") {
              setSelectedPolicyIndex(Math.max(selectedPolicyIndex - 1, 0));
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
          t: () => {
            setShowTransfer(true);
          },
          r: () => refreshCurrentView(),
          c: () => {
            if (activeTab !== "reservations" || !client) return;
            const selected = reservations[selectedReservationIndex];
            if (selected && selected.status === "pending") {
              commitReservation(selected.id, client);
            }
          },
          x: async () => {
            if (activeTab !== "reservations" || !client) return;
            const selected = reservations[selectedReservationIndex];
            if (selected && selected.status === "pending") {
              const ok = await confirm("Release reservation?", `Release reservation ${selected.id}. Reserved funds will be returned.`);
              if (!ok) return;
              releaseReservation(selected.id, client);
            }
          },
          d: async () => {
            if (activeTab !== "policies" || !client) return;
            const selected = policies[selectedPolicyIndex];
            if (selected) {
              const ok = await confirm("Delete policy?", "Delete spending policy. This cannot be undone.");
              if (!ok) return;
              deletePolicy(selected.policy_id, client);
            }
          },
          b: () => {
            if (activeTab !== "policies" || !client) return;
            fetchBudget(client);
          },
          n: () => {
            if (activeTab !== "transactions" || !client) return;
            fetchNextTransactions(client);
          },
          p: () => {
            if (activeTab !== "transactions" || !client) return;
            fetchPrevTransactions(client);
          },
          i: () => {
            if (activeTab !== "transactions" || !client) return;
            const selected = transactions[selectedTransactionIndex];
            if (selected) {
              verifyIntegrity(selected.id, client);
            }
          },
          a: () => {
            if (activeTab === "balance") {
              setAffordInputMode(true);
              setAffordBuffer("");
            }
          },
          "shift+n": () => {
            if (activeTab === "policies") {
              setPolicyInputMode(true);
              setPolicyBuffer("");
            }
          },
        },
    (affordInputMode || policyInputMode) ? (keyName: string) => {
      if (affordInputMode && keyName.length === 1 && /[\d.]/.test(keyName)) {
        setAffordBuffer((b) => b + keyName);
      } else if (policyInputMode && keyName.length === 1) {
        setPolicyBuffer((b) => b + keyName);
      } else if (policyInputMode && keyName === "space") {
        setPolicyBuffer((b) => b + " ");
      }
    } : undefined,
  );

  return (
    <BrickGate brick="pay">
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

        {/* Afford check input */}
        {affordInputMode && (
          <box height={1} width="100%">
            <text>{`Check afford amount: ${affordBuffer}\u2588  (Enter:check  Escape:cancel)`}</text>
          </box>
        )}

        {/* Policy create input */}
        {policyInputMode && (
          <box height={1} width="100%">
            <text>{`New policy name: ${policyBuffer}\u2588  (Enter:create  Escape:cancel)`}</text>
          </box>
        )}

        {/* Error display */}
        {error && (
          <box height={1} width="100%">
            <text>{`Error: ${error}`}</text>
          </box>
        )}

        {/* Detail content */}
        <box flexGrow={1} borderStyle="single">
          {showTransfer ? (
            <TransferForm
              onSubmit={handleTransferSubmit}
              onCancel={handleTransferCancel}
            />
          ) : (
            <>
              {activeTab === "balance" && (
                <>
                  <BalanceCard balance={balance} loading={balanceLoading} />
                  {affordResult && (
                    <box height={1} width="100%" marginTop={1}>
                      <text>
                        {`Afford check: ${affordResult.can_afford ? "YES" : "NO"} (balance=${affordResult.balance} requested=${affordResult.requested})`}
                      </text>
                    </box>
                  )}
                </>
              )}
              {activeTab === "reservations" && (
                <ReservationList
                  reservations={reservations}
                  selectedIndex={selectedReservationIndex}
                  loading={reservationsLoading}
                />
              )}
              {activeTab === "transactions" && (
                <TransactionList
                  transactions={transactions}
                  selectedIndex={selectedTransactionIndex}
                  loading={transactionsLoading}
                  hasMore={transactionsHasMore}
                  hasPrev={transactionsCursorStack.length > 0}
                  integrityResult={integrityResult}
                />
              )}
              {activeTab === "policies" && (
                <box flexDirection="column" height="100%" width="100%">
                  <BudgetCard budget={budget} loading={budgetLoading} />
                  <PolicyList
                    policies={policies}
                    selectedIndex={selectedPolicyIndex}
                    loading={policiesLoading}
                  />
                </box>
              )}
            </>
          )}
        </box>

        {/* Help bar */}
        <box height={1} width="100%">
          <text>
            {showTransfer
              ? "Tab:next field  Enter:submit  Escape:cancel"
              : activeTab === "transactions"
                ? "j/k:navigate  n:next page  p:prev page  i:verify integrity  Tab:switch tab  r:refresh"
                : activeTab === "policies"
                  ? "j/k:navigate  Tab:switch tab  Shift+N:new  d:delete  b:budget  r:refresh  q:quit"
                  : activeTab === "balance"
                  ? "Tab:switch tab  t:transfer  a:afford check  r:refresh  q:quit"
                  : "j/k:navigate  Tab:switch tab  t:transfer  r:refresh  c:commit  x:release  q:quit"}
          </text>
        </box>
      </box>
    </BrickGate>
  );
}
