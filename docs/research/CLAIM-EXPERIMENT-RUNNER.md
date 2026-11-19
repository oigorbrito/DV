# Claim experiment runner (CI gate)

**Status:** gate and accounting report implemented; **no arm can execute yet**. The workflow `.github/workflows/run-claim-experiment.yml` validates and plans. Execute-requested runs fail closed.

## Why it does not execute today

- `experiments/p1/wp07-treatment-bindings-v1.json` lists every treatment B0–B4 as `UNBOUND`. The file itself states that a blocked cell is not a run.
- `experiments/p1/claim-experiment-registry-v1.json` has no frozen design (`designs: {}`). No claim is linked to a corpus slice, verifier pin, run count or budget estimate.

Creating a design or binding is a research decision (new frozen revision), not a CI change.

## Reuse map (REUSE → WRAP)

| Need | Reused source | Used as |
|---|---|---|
| Claim identity and status | `claims/claims-v1.json` | must exist and be `UNTESTED` |
| Treatment identity | `experiments/p1/wp07-treatment-bindings-v1.json` | must be `BOUND` with bindings |
| Accounting partitions and metrics | `experiments/p1/run-accounting-contract-v1.json` | partition keys, primary metric, failure cost rule |
| Fail-closed execution token pattern | `tools/ndv_run_wp07_codex_single_hop.py` (`--execute-token`) | same idea: no execution without explicit request |
| Runner outputs (for a future adapter) | `classification.verified_solved_task`, `usage.total_system_tokens_component`, `wall_seconds`, failure cost retained | to be mapped into `dv-claim-arm-rows-v1` |

Nothing in `tools/ndv_*`, `experiments/`, `claims/` or `provenance/` was modified.

## Components

- `tools/dv_claim_experiment_gate.py`: validates the request (claim, arms, slice, budget ceiling 10.00 USD), checks the frozen design, corpus pin, verifier pins, binding status and estimated spend. Dry run by default. `--execute` requires `DV_EXECUTOR_API_KEY` (presence only, never printed). `--verify-manifest` re-hashes oracle files recorded in a plan.
- `tools/dv_claim_experiment_report.py`: summarizes normalized arm rows (`dv-claim-arm-rows-v1`). Only `VERIFIED_SOLVED` counts as solved. Missing telemetry yields `null` and blocks the primary metric; it is never zero. Pending verification blocks the primary metric. Failed, inconclusive and pending runs keep their cost. Output decision is always `NOT_DECIDED`. No composite score.
- `experiments/p1/claim-experiment-registry-v1.json`: registry of frozen designs (empty).
- `.github/workflows/run-claim-experiment.yml`: `workflow_dispatch` only, `permissions: contents: read`, inputs `claim_id`, `baseline_setting`, `treatment_setting`, `corpus_slice`, `max_budget_usd` (default `2.00`), `dry_run` (default `true`). Writes the plan to the step summary, re-checks oracle hashes, and uploads `artifacts/`.

## Exit and status vocabulary

| Situation | Status | Exit |
|---|---|---|
| Invalid input | `INVALID_INPUT` | 2 |
| Dry run, any plan | `PLAN_VALID_DRY_RUN` or `PLAN_BLOCKED_DRY_RUN` | 0 |
| Execute requested, local GAP | `BLOCKED_GAP` | 1 |
| Execute requested, credential missing | `UNKNOWN_EXTERNAL` (takes precedence over GAP) | 3 |
| Execute requested, all checks pass | `READY_FOR_EXECUTION` (workflow then fails closed: no adapter) | 0, then the adapter step fails |

## Adding a design (future revision, not automatic)

1. Freeze the corpus slice under `experiments/p1/` and record its sha256.
2. Pin the verifier and oracle files (`verifier_files`), not editable during execution.
3. Bind the treatments in the bindings registry (`status: BOUND`, binding file sha256).
4. Set `runs_per_arm` and `estimated_usd_per_run` for each arm.
5. Implement an execution adapter that writes `arm-rows.json` (`dv-claim-arm-rows-v1`), then review the resulting summary by a human. The workflow never merges or promotes a claim.

## Non-goals

- No automatic claim promotion, no auto-merge, no composite score.
- No provider credentials in the repository or in logs.
- No attempt to reproduce upstream benchmarks; benchmark version preferences are in `docs/research/EXTERNAL-FINDINGS-2026.md`.
