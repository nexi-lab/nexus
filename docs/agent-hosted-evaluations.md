# Durable hosted evaluation records

The experimental Registry profile now persists evaluation runs and case results.
These are migration evaluation records, not a new generic Task authority. They do
not change publication, channels, or an asset's L0–L3 admission grade.

An explicitly configured IAM evaluator/gateway starts a run tied to the current
approved internal-hosted asset, immutable artifact/spec, dataset/rubric and initiating
user. Each dataset case remains in the denominator, including errors and unknown
results. The owner computes exact ASCII-whitespace-trimmed matching; clients cannot
submit a score. Trace frames are bounded, persisted and hashed by the owner, and
are labelled worker-reported. Model-call references are evaluator-reported links;
the authoritative model ledger remains Router.

Only one case per run can be claimed. The claim is durable before IAM/model/container
side effects, with a one-time random lease whose hash alone is stored. Started cases
are never reclaimed. A deterministic IAM request ID also identifies an issuance that
crashed before its grant ID was bound back to the evaluation record. Successful
completion revokes the actual IAM grant and requires confirmed cleanup. Timeout
reconciliation and stopping are explicit POST operations. GET and regular startup
never migrate, publish, stop, or replay work. No lease or worker credential is exposed
by list/get APIs; evaluator endpoints use no-store.

Stopping closes queued cases and records running cases as unknown while revoking IAM.
It does not claim Docker cancellation. Unknown cleanup blocks further claims within
that run; reconcile can retry IAM cleanup but cannot invent a container cleanup
receipt. The trusted supervisor and deployment operator remain responsible for
container cleanup. There is an atomic limit of 200 queued/running cases per asset.

Prepare the additive tables explicitly with the existing Registry schema initialization
command after taking the deployment backup. Production startup does not initialize
the schema. The evaluator must be provisioned in actual IAM and listed in both
`evaluators` and `gateways` for its zone; an admin flag alone does not grant evaluator
access. Browser managers use the authenticated Moss gateway.

Validation includes real file-backed Registry/IAM, concurrent claims, immutable
request replay, key omission on replay, cross-zone/role denials, read-only queries,
all-case scoring, stopping, timeout and IAM revocation. This does not constitute
HA, migration comparison, L2 publication or full Agent Management 1.1 acceptance.

## Source baselines and immutable comparison policy

The same durable case lifecycle distinguishes `source_baseline` from `hosted`.
A baseline records a deployment-selected source runtime snapshot. Its source digest,
platform profile and model must match the imported artifact. A Dify export is labelled
an observed snapshot; it is never described as a remote immutable execution version.
The evaluator must actually invoke the original platform/runtime for that role.

After all baseline cases settle with confirmed cleanup, an evaluator can create an
immutable policy specifying minimum score and maximum drop in integer basis points.
The candidate run must reference that existing policy at creation. A completed run
cannot be retrospectively attached to a new threshold. The owner calculates both
scores from stored cases with the full dataset denominator, checks exact metadata,
artifact, dataset and rubric bindings, and persists an idempotent comparison. Unknown
outcomes or pending cleanup block comparison. A policy's existence does not grant L2.

API additions: `/hosted/migration/policies`, `/hosted/migration/policies/{id}`,
`/hosted/migration/comparisons/{id}` and
`POST /hosted/evaluations/{candidate_run_id}/compare`, under the existing product
registry API prefix. These are evaluator-only and no-store. Policy and comparison
tables are additive and require the same explicit initialization maintenance step.
Existing default hosted start-request fingerprints are preserved across this upgrade.

The owner lifecycle/threshold math is implemented and tested. Actual source-runner
product orchestration, manager policy UI and L2 evidence admission are separate work;
consumers without a source adapter must reject that role instead of executing the
imported image. The independent laboratory source report alone is not imported as
a trusted policy or acceptance record.
