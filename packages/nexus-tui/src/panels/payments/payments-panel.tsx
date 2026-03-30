/**
 * Payments panel: tabbed layout for Balance, Reservations, Transactions,
 * Policies, and Approvals views.
 */

import React, { useState, useCallback, useEffect } from "react";
import { usePaymentsStore } from "../../stores/payments-store.js";
import type { PaymentsTab } from "../../stores/payments-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useCopy } from "../../shared/hooks/use-copy.js";
import { jumpToStart, jumpToEnd } from "../../shared/hooks/use-list-navigation.js";
import { useConfirmStore } from "../../shared/hooks/use-confirm.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { useUiStore } from "../../stores/ui-store.js";
import { useVisibleTabs, type TabDef } from "../../shared/hooks/use-visible-tabs.js";
import { SubTabBar } from "../../shared/components/sub-tab-bar.js";
import { subTabCycleBindings } from "../../shared/components/sub-tab-bar-utils.js";
import { useTabFallback } from "../../shared/hooks/use-tab-fallback.js";
import { BrickGate } from "../../shared/components/brick-gate.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";
import { BalanceCard } from "./balance-card.js";
import { ReservationList } from "./reservation-list.js";
import { TransferForm } from "./transfer-form.js";
import { TransactionList } from "./transaction-list.js";
import { PolicyList } from "./policy-list.js";
import { BudgetCard } from "./budget-card.js";
import { ApprovalList } from "./approval-list.js";

const ALL_TABS: readonly TabDef<PaymentsTab>[] = [
  { id: "balance", label: "Balance", brick: null },
  { id: "reservations", label: "Reservations", brick: null },
  { id: "transactions", label: "Transactions", brick: null },
  { id: "policies", label: "Policies", brick: null },
  { id: "approvals", label: "Approvals", brick: null },
];

export default function PaymentsPanel(): React.ReactNode {
  const client = useApi();
  const confirm = useConfirmStore((s) => s.confirm);
  const overlayActive = useUiStore((s) => s.overlayActive);
  const { copy, copied } = useCopy();
  const [showTransfer, setShowTransfer] = useState(false);
  const [affordInputMode, setAffordInputMode] = useState(false);
  const [affordBuffer, setAffordBuffer] = useState("");
  const [policyInputMode, setPolicyInputMode] = useState(false);
  const [policyBuffer, setPolicyBuffer] = useState("");
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

  const visibleTabs = useVisibleTabs(ALL_TABS);
  useTabFallback(visibleTabs, activeTab, setActiveTab);

  const setSelectedReservationIndex = usePaymentsStore(
    (s) => s.setSelectedReservationIndex,
  );
  const setSelectedTransactionIndex = usePaymentsStore(
    (s) => s.setSelectedTransactionIndex,
  );
  const [selectedPolicyIndex, setSelectedPolicyIndex] = useState(0);

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
  const refreshCurrentView = (): void => {
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
  };

  // Auto-fetch when tab changes
  useEffect(() => {
    refreshCurrentView();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, client]);

  useKeyboard(
    overlayActive
      ? {}
      : showTransfer
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
          j: () => {
            if (activeTab === "reservations") {
              if (reservations.length === 0) return;
              setSelectedReservationIndex(
                Math.max(0, Math.min(selectedReservationIndex + 1, reservations.length - 1)),
              );
            } else if (activeTab === "transactions") {
              if (transactions.length === 0) return;
              setSelectedTransactionIndex(
                Math.max(0, Math.min(selectedTransactionIndex + 1, transactions.length - 1)),
              );
            } else if (activeTab === "policies") {
              if (policies.length === 0) return;
              setSelectedPolicyIndex(
                Math.max(0, Math.min(selectedPolicyIndex + 1, policies.length - 1)),
              );
            } else if (activeTab === "approvals") {
              if (approvals.length === 0) return;
              setSelectedApprovalIndex(
                Math.max(0, Math.min(selectedApprovalIndex + 1, approvals.length - 1)),
              );
            }
          },
          down: () => {
            if (activeTab === "reservations") {
              if (reservations.length === 0) return;
              setSelectedReservationIndex(
                Math.max(0, Math.min(selectedReservationIndex + 1, reservations.length - 1)),
              );
            } else if (activeTab === "transactions") {
              if (transactions.length === 0) return;
              setSelectedTransactionIndex(
                Math.max(0, Math.min(selectedTransactionIndex + 1, transactions.length - 1)),
              );
            } else if (activeTab === "policies") {
              if (policies.length === 0) return;
              setSelectedPolicyIndex(
                Math.max(0, Math.min(selectedPolicyIndex + 1, policies.length - 1)),
              );
            } else if (activeTab === "approvals") {
              if (approvals.length === 0) return;
              setSelectedApprovalIndex(
                Math.max(0, Math.min(selectedApprovalIndex + 1, approvals.length - 1)),
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
            } else if (activeTab === "approvals") {
              setSelectedApprovalIndex(Math.max(selectedApprovalIndex - 1, 0));
            }
          },
          up: () => {
            if (activeTab === "reservations") {
              setSelectedReservationIndex(Math.max(selectedReservationIndex - 1, 0));
            } else if (activeTab === "transactions") {
              setSelectedTransactionIndex(Math.max(selectedTransactionIndex - 1, 0));
            } else if (activeTab === "policies") {
              setSelectedPolicyIndex(Math.max(selectedPolicyIndex - 1, 0));
            } else if (activeTab === "approvals") {
              setSelectedApprovalIndex(Math.max(selectedApprovalIndex - 1, 0));
            }
          },
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
              setAffordInputMode(true);
              setAffordBuffer("");
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
              setPolicyInputMode(true);
              setPolicyBuffer("");
            }
          },
          g: () => {
            if (activeTab === "reservations") {
              setSelectedReservationIndex(jumpToStart());
            } else if (activeTab === "transactions") {
              setSelectedTransactionIndex(jumpToStart());
            } else if (activeTab === "policies") {
              setSelectedPolicyIndex(jumpToStart());
            } else if (activeTab === "approvals") {
              setSelectedApprovalIndex(jumpToStart());
            }
          },
          "shift+g": () => {
            if (activeTab === "reservations") {
              setSelectedReservationIndex(jumpToEnd(reservations.length));
            } else if (activeTab === "transactions") {
              setSelectedTransactionIndex(jumpToEnd(transactions.length));
            } else if (activeTab === "policies") {
              setSelectedPolicyIndex(jumpToEnd(policies.length));
            } else if (activeTab === "approvals") {
              setSelectedApprovalIndex(jumpToEnd(approvals.length));
            }
          },
          y: () => {
            if (activeTab === "transactions") {
              const selected = transactions[selectedTransactionIndex];
              if (selected) copy(selected.id);
            }
          },
        },
    (!overlayActive && (affordInputMode || policyInputMode || approvalInputMode)) ? (keyName: string) => {
      if (affordInputMode && keyName.length === 1 && /[\d.]/.test(keyName)) {
        setAffordBuffer((b) => b + keyName);
      } else if (policyInputMode && keyName.length === 1) {
        setPolicyBuffer((b) => b + keyName);
      } else if (policyInputMode && keyName === "space") {
        setPolicyBuffer((b) => b + " ");
      } else if (approvalInputMode && approvalField === "amount" && keyName.length === 1 && /[\d.]/.test(keyName)) {
        setApprovalAmountBuffer((b) => b + keyName);
      } else if (approvalInputMode && approvalField === "purpose" && keyName.length === 1) {
        setApprovalPurposeBuffer((b) => b + keyName);
      } else if (approvalInputMode && approvalField === "purpose" && keyName === "space") {
        setApprovalPurposeBuffer((b) => b + " ");
      }
    } : undefined,
  );

  return (
    <BrickGate brick="pay">
      <box height="100%" width="100%" flexDirection="column">
        {/* Tab bar */}
        <SubTabBar tabs={visibleTabs} activeTab={activeTab} />

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

        {/* Approval request input */}
        {approvalInputMode && (
          <box flexDirection="column" width="100%">
            <box height={1} width="100%">
              <text>{approvalField === "amount" ? `> Amount:  ${approvalAmountBuffer}\u2588` : `  Amount:  ${approvalAmountBuffer}`}</text>
            </box>
            <box height={1} width="100%">
              <text>{approvalField === "purpose" ? `> Purpose: ${approvalPurposeBuffer}\u2588` : `  Purpose: ${approvalPurposeBuffer}`}</text>
            </box>
            <box height={1} width="100%">
              <text>{"  Tab:next field  Enter:submit  Escape:cancel"}</text>
            </box>
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
            ? <text foregroundColor="green">Copied!</text>
            : <text>
            {showTransfer
              ? "Tab:next field  Enter:submit  Escape:cancel"
              : activeTab === "transactions"
                ? "j/k:navigate  ]:next  [:prev  i:verify integrity  y:copy  Tab:switch tab  r:refresh"
                : activeTab === "policies"
                  ? "j/k:navigate  Tab:switch tab  Shift+N:new  d:delete  b:budget  r:refresh  q:quit"
                  : activeTab === "balance"
                    ? "Tab:switch tab  t:transfer  a:afford check  r:refresh  q:quit"
                    : activeTab === "approvals"
                      ? "j/k:navigate  n:new request  a:approve  x:reject  Tab:switch tab  r:refresh  q:quit"
                      : "j/k:navigate  Tab:switch tab  t:transfer  r:refresh  c:commit  x:release  q:quit"}
          </text>}
        </box>
      </box>
    </BrickGate>
  );
}
