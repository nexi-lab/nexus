/**
 * Payments panel: tabbed layout for Balance, Reservations, Transactions,
 * Policies, and Approvals views.
 */

import React, { useState, useCallback, useEffect } from "react";
import { usePaymentsStore } from "../../stores/payments-store.js";
import type { PaymentsTab } from "../../stores/payments-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useCopy } from "../../shared/hooks/use-copy.js";
import { listNavigationBindings } from "../../shared/hooks/use-list-navigation.js";
import { useTextInput } from "../../shared/hooks/use-text-input.js";
import { useConfirmStore } from "../../shared/hooks/use-confirm.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { useUiStore } from "../../stores/ui-store.js";
import { useVisibleTabs } from "../../shared/hooks/use-visible-tabs.js";
import { useTabFallback } from "../../shared/hooks/use-tab-fallback.js";
import { SubTabBar } from "../../shared/components/sub-tab-bar.js";
import { subTabCycleBindings } from "../../shared/components/sub-tab-bar-utils.js";
import { BrickGate } from "../../shared/components/brick-gate.js";
import { statusColor } from "../../shared/theme.js";
import { BalanceCard } from "./balance-card.js";
import { ReservationList } from "./reservation-list.js";
import { TransferForm } from "./transfer-form.js";
import { TransactionList } from "./transaction-list.js";
import { PolicyList } from "./policy-list.js";
import { BudgetCard } from "./budget-card.js";
import { ApprovalList } from "./approval-list.js";
import { PAYMENTS_TABS } from "../../shared/navigation.js";
import { textStyle } from "../../shared/text-style.js";

const HELP_TEXT: Readonly<Record<string, string>> = {
  balance: "Tab:switch tab  t:transfer  a:afford check  r:refresh  q:quit",
  reservations: "j/k:navigate  Tab:switch tab  t:transfer  r:refresh  c:commit  x:release  q:quit",
  transactions: "j/k:navigate  ]:next  [:prev  i:verify integrity  y:copy  Tab:switch tab  r:refresh",
  policies: "j/k:navigate  Tab:switch tab  Shift+N:new  d:delete  b:budget  r:refresh  q:quit",
  approvals: "j/k:navigate  n:new request  a:approve  x:reject  Tab:switch tab  r:refresh  q:quit",
};

export default function PaymentsPanel(): React.ReactNode {
  const client = useApi();
  const confirm = useConfirmStore((s) => s.confirm);
  const overlayActive = useUiStore((s) => s.overlayActive);
  const { copy, copied } = useCopy();
  const [showTransfer, setShowTransfer] = useState(false);
  const [approvalInputMode, setApprovalInputMode] = useState(false);
  const [approvalAmountBuffer, setApprovalAmountBuffer] = useState("");
  const [approvalPurposeBuffer, setApprovalPurposeBuffer] = useState("");
  const [approvalField, setApprovalField] = useState<"amount" | "purpose">("amount");

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
  const approvals = usePaymentsStore((s) => s.approvals);
  const approvalsLoading = usePaymentsStore((s) => s.approvalsLoading);
  const selectedApprovalIndex = usePaymentsStore((s) => s.selectedApprovalIndex);
  const fetchApprovals = usePaymentsStore((s) => s.fetchApprovals);
  const requestApproval = usePaymentsStore((s) => s.requestApproval);
  const approveRequest = usePaymentsStore((s) => s.approveRequest);
  const rejectRequest = usePaymentsStore((s) => s.rejectRequest);
  const setSelectedApprovalIndex = usePaymentsStore((s) => s.setSelectedApprovalIndex);
  const setActiveTab = usePaymentsStore((s) => s.setActiveTab);
  const setSelectedReservationIndex = usePaymentsStore(
    (s) => s.setSelectedReservationIndex,
  );
  const setSelectedTransactionIndex = usePaymentsStore(
    (s) => s.setSelectedTransactionIndex,
  );
  const [selectedPolicyIndex, setSelectedPolicyIndex] = useState(0);

  const visibleTabs = useVisibleTabs(PAYMENTS_TABS);
  useTabFallback(visibleTabs, activeTab, setActiveTab);

  // Clamp selectedPolicyIndex when policies list shrinks (e.g. after delete)
  useEffect(() => {
    if (policies.length > 0 && selectedPolicyIndex >= policies.length) {
      setSelectedPolicyIndex(Math.max(0, policies.length - 1));
    }
  }, [policies.length, selectedPolicyIndex]);

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
  const refreshCurrentView = useCallback((): void => {
    if (!client) return;

    if (activeTab === "balance") {
      fetchBalance(client);
    } else if (activeTab === "transactions") {
      fetchTransactions(client);
    } else if (activeTab === "policies") {
      fetchPolicies(client);
    } else if (activeTab === "approvals") {
      fetchApprovals(client);
    }
  }, [client, activeTab, fetchBalance, fetchTransactions, fetchPolicies, fetchApprovals]);

  // Auto-fetch when tab changes
  useEffect(() => {
    refreshCurrentView();
  }, [refreshCurrentView]);

  // Text input for afford check (numbers-only)
  const affordInput = useTextInput({
    onSubmit: (val) => {
      if (val && client) checkAfford(val, client);
    },
    filter: (ch) => /[\d.]/.test(ch),
  });

  // Text input for policy name creation
  const policyInput = useTextInput({
    onSubmit: (val) => {
      if (val && client) createPolicy(val, {}, client);
    },
  });

  // Shared list navigation (j/k/up/down/g/G) — switches per active tab
  const listNav = listNavigationBindings({
    getIndex: () => {
      if (activeTab === "reservations") return selectedReservationIndex;
      if (activeTab === "transactions") return selectedTransactionIndex;
      if (activeTab === "policies") return selectedPolicyIndex;
      if (activeTab === "approvals") return selectedApprovalIndex;
      return 0;
    },
    setIndex: (i) => {
      if (activeTab === "reservations") setSelectedReservationIndex(i);
      else if (activeTab === "transactions") setSelectedTransactionIndex(i);
      else if (activeTab === "policies") setSelectedPolicyIndex(i);
      else if (activeTab === "approvals") setSelectedApprovalIndex(i);
    },
    getLength: () => {
      if (activeTab === "reservations") return reservations.length;
      if (activeTab === "transactions") return transactions.length;
      if (activeTab === "policies") return policies.length;
      if (activeTab === "approvals") return approvals.length;
      return 0;
    },
  });

  // Determine which input mode (if any) is active for useKeyboard routing
  const anyInputActive = affordInput.active || policyInput.active || approvalInputMode;

  useKeyboard(
    overlayActive
      ? {}
      : showTransfer
      ? {}
      : affordInput.active
        ? affordInput.inputBindings
        : policyInput.active
          ? policyInput.inputBindings
          : approvalInputMode
            ? {
                return: () => {
                  const amount = parseFloat(approvalAmountBuffer.trim());
                  const purpose = approvalPurposeBuffer.trim();
                  if (Number.isFinite(amount) && purpose && client) {
                    requestApproval(amount, purpose, client);
                  }
                  setApprovalInputMode(false);
                  setApprovalAmountBuffer("");
                  setApprovalPurposeBuffer("");
                  setApprovalField("amount");
                },
                escape: () => {
                  setApprovalInputMode(false);
                  setApprovalAmountBuffer("");
                  setApprovalPurposeBuffer("");
                  setApprovalField("amount");
                },
                backspace: () => {
                  if (approvalField === "amount") {
                    setApprovalAmountBuffer((b) => b.slice(0, -1));
                  } else {
                    setApprovalPurposeBuffer((b) => b.slice(0, -1));
                  }
                },
                tab: () => {
                  setApprovalField((f) => f === "amount" ? "purpose" : "amount");
                },
              }
            : {
          ...listNav,
          ...subTabCycleBindings(visibleTabs, activeTab, setActiveTab),
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
            if (activeTab === "reservations" && client) {
              const selected = reservations[selectedReservationIndex];
              if (selected && selected.status === "pending") {
                const ok = await confirm("Release reservation?", `Release reservation ${selected.id}. Reserved funds will be returned.`);
                if (!ok) return;
                releaseReservation(selected.id, client);
              }
            } else if (activeTab === "approvals" && client) {
              const selected = approvals[selectedApprovalIndex];
              if (selected && selected.status === "pending") {
                const ok = await confirm("Reject approval?", `Reject spending approval request ${selected.id}.`);
                if (!ok) return;
                rejectRequest(selected.id, client);
              }
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
          "]": () => {
            if (activeTab !== "transactions" || !client) return;
            fetchNextTransactions(client);
          },
          "[": () => {
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
          a: async () => {
            if (activeTab === "balance") {
              affordInput.activate();
            } else if (activeTab === "approvals" && client) {
              const selected = approvals[selectedApprovalIndex];
              if (selected && selected.status === "pending") {
                const ok = await confirm("Approve request?", `Approve spending request ${selected.id} for ${selected.amount}.`);
                if (!ok) return;
                approveRequest(selected.id, client);
              }
            }
          },
          n: () => {
            if (activeTab === "approvals") {
              setApprovalInputMode(true);
              setApprovalAmountBuffer("");
              setApprovalPurposeBuffer("");
              setApprovalField("amount");
            }
          },
          "shift+n": () => {
            if (activeTab === "policies") {
              policyInput.activate();
            }
          },
          y: () => {
            if (activeTab === "transactions") {
              const selected = transactions[selectedTransactionIndex];
              if (selected) copy(selected.id);
            }
          },
        },
    !overlayActive && anyInputActive
      ? affordInput.active
        ? affordInput.onUnhandled
        : policyInput.active
          ? policyInput.onUnhandled
          : approvalInputMode
            ? (keyName: string) => {
                if (approvalField === "amount" && keyName.length === 1 && /[\d.]/.test(keyName)) {
                  setApprovalAmountBuffer((b) => b + keyName);
                } else if (approvalField === "purpose" && keyName.length === 1) {
                  setApprovalPurposeBuffer((b) => b + keyName);
                } else if (approvalField === "purpose" && keyName === "space") {
                  setApprovalPurposeBuffer((b) => b + " ");
                }
              }
            : undefined
      : undefined,
  );

  return (
    <BrickGate brick="pay">
      <box height="100%" width="100%" flexDirection="column">
        {/* Tab bar */}
        <SubTabBar tabs={visibleTabs} activeTab={activeTab} />

        {/* Afford check input */}
        {affordInput.active && (
          <box height={1} width="100%">
            <text>{`Check afford amount: ${affordInput.buffer}\u2588  (Enter:check  Escape:cancel)`}</text>
          </box>
        )}

        {/* Policy create input */}
        {policyInput.active && (
          <box height={1} width="100%">
            <text>{`New policy name: ${policyInput.buffer}\u2588  (Enter:create  Escape:cancel)`}</text>
          </box>
        )}

        {/* Approval request input (inline bar) */}
        {approvalInputMode && (
          <box height={1} width="100%">
            <text>
              {approvalField === "amount"
                ? `Amount: ${approvalAmountBuffer}\u2588 \u2502 Purpose: ${approvalPurposeBuffer}  (Tab:next  Enter:submit  Esc:cancel)`
                : `Amount: ${approvalAmountBuffer} \u2502 Purpose: ${approvalPurposeBuffer}\u2588  (Tab:next  Enter:submit  Esc:cancel)`}
            </text>
          </box>
        )}

        {/* Error display — 404 means the pay API routes aren't registered */}
        {error && (
          <box height={1} width="100%">
            <text>{error.includes("Not Found") || error.includes("404")
              ? "Payment APIs not available on this server. The pay brick is enabled but REST routes are not registered."
              : `Error: ${error}`}</text>
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
                  currentPage={transactionsCursorStack.length + 1}
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
              {activeTab === "approvals" && (
                <ApprovalList
                  approvals={approvals}
                  selectedIndex={selectedApprovalIndex}
                  loading={approvalsLoading}
                />
              )}
            </>
          )}
        </box>

        {/* Help bar */}
        <box height={1} width="100%">
          {copied
            ? <text style={textStyle({ fg: "green" })}>Copied!</text>
            : <text>
            {showTransfer
              ? "Tab:next field  Enter:submit  Escape:cancel"
              : HELP_TEXT[activeTab] ?? "j/k:navigate  Tab:switch tab  r:refresh  q:quit"}
          </text>}
        </box>
      </box>
    </BrickGate>
  );
}
