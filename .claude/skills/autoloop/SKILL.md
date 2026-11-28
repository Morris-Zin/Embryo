---
name: autoloop
description: Opt-in autonomous engineering workflow loop combining /grill-me + /grill-with-docs spec interviews, frozen scope contracts, serialized multi-shard execution, independent review, and layered safeguards.
disable-model-invocation: true
---

# `/autoloop` — Opt-In Autonomous Workflow Loop

## Hard Opt-In Guard

- **Never activate automatically** on ordinary coding, debugging, review, or research prompts.
- Activate **only** when the user explicitly types `/autoloop <goal>` or explicitly names an active `.autoloop/runs/<run_id>.json` checkpoint.
- Ordinary PRs remain unaffected and skip candidate `fast-check` jobs unless enrolled.

## Phase 1: Spec Interview (`/grill-me` + `/grill-with-docs`)

Before locking any contract or writing implementation code:

1. Read repo context (`AGENTS.md`, `GLOSSARY.md` if present, relevant `docs/adr/` entries, and `docs/agents/routing.md`).
2. Read matching lessons before and during the interview. Run `lessons --paths 'apps/khitmee_ops/**'` with tentative shard globs. `init --paths <glob>` also prints matches (all lessons if paths are omitted), and `complete-grill` prints matches for the agreed shard paths. Run a focused `/grill-me` + `/grill-with-docs` interview to resolve ambiguity, edge cases, non-goals, acceptance tests, and domain vocabulary.
3. Update `GLOSSARY.md` and `docs/adr/` lazily when new domain terms or architectural decisions are resolved during the interview.
4. Confirm the user's preferred **model roster** (defaulting to `docs/agents/routing.md` presets in `.autoloop/config.json`) and reviewer configuration (`cloud`, `deep`, or explicitly disabled).
5. Confirm whether merge mode is `human` (default) or opt-in `auto` (which still requires owner-applied `allow-auto-merge` label + approval comment).

Save confirmed lessons with `record-lesson --run-id <id> --kind process --summary "<lesson>" --evidence <path-or-link> --applies-to '<glob>'`. Kinds are `dead_end`, `review_finding`, `safeguard`, `perf_insight`, and `process`. Commit confirmed lessons under `.autoloop/lessons/` within approved scope so other worktrees and future runs can read them. `log-dead-end` and `record-safeguard` produce local drafts under the existing ignored `.autoloop/tmp/lessons/` directory. Lesson lookup includes these drafts. Use `record-lesson` to publish a confirmed version.

## Phase 2: Freeze & Publish Contract (`FrozenContractV1`)

1. Initialize the run (`draft`):
   `python tools/autoloop/cli.py init --run-id <id> --goal "<goal>" --issue <N>`
2. Record interview decisions and non-overlapping shard `allowed_paths` (`grilled`):
   `python tools/autoloop/cli.py complete-grill --run-id <id> --summary "<summary>" --shard shard_a=<globs> [--shard-check shard_a="<cmd>"] [--allow-policy-path <glob>]`
3. Freeze `FrozenContractV1` (`locked`):
   `python tools/autoloop/cli.py lock --run-id <id> [--pr <N>]`
4. Bind a PR explicitly before opening/finalizing (or after PR creation):
   `python tools/autoloop/cli.py bind-pr --run-id <id> --pr <N>`
5. Publish the owner-authenticated contract comment to the GitHub Issue (or PR):
   `python tools/autoloop/cli.py publish-contract --run-id <id>`

## Phase 3: Serialized Multi-Shard Execution & Bounded Retries

1. Delegate bounded shards to the agreed worker models, following `docs/agents/routing.md`. Give each worker its own worktree. Acquire and renew leases before using shared live stacks, databases, or hosts. Use `--holder-pid <pid>` to bind a lease to a long-lived worker PID or use TTL with a secret `--token`. Provide the token on `renew` and `release`.
   `python tools/autoloop/cli.py lease acquire --resource <key> --owner <worker> [--holder-pid <pid>]`
   `python tools/autoloop/cli.py lease release --resource <key> --owner <worker> --token <lease_token>`
   Inspect ownership with `lease status --resource <key>`. Worktrees share the Git common directory's `autoloop-locks` unless `AUTOLOOP_LOCK_DIR` is set. On a Windows-backed WSL checkout, run lease commands from Windows PowerShell using `python.exe` and the same checkout. For a WSL holder, use a token and TTL without a Linux PID. Drain active workers before migrating from the old temporary lock store.
2. Start each shard (`start-shard` supports concurrent multi-shard execution while `run.status` is `locked` or `in_progress`):
   `python tools/autoloop/cli.py start-shard --run-id <id> --shard <shard_id> --worktree <path>`
3. Commit changes cleanly inside the shard worktree, then verify shard-local boundaries, static rules, and tests (`check` inspects the clean registered shard worktree automatically and refuses dirty/mismatched checkouts):
   `python tools/autoloop/cli.py check --run-id <id> --shard <shard_id>`
   Invoke `check` through the trusted coordinator checkout. The tooling snapshot uses the recorded run base and owner-scoped tooling changes from individually passed policy shards. A shard's own changes cannot supply its gate. An attempt using `--skip-fast-check` cannot promote tooling. The checkpoint records the tooling digest and source commits. Frozen shard commands still run inside their shard worktrees.
   Run `doctor` to diagnose Python, the pinned dev group, uv, Docker, and KhitmeeERPNext on Windows. It prints repair commands without installing anything. Investigate steps taking over 60 seconds using the slow-step output and the last five timing records per scope, including the `fast_check.py` job timings.
4. For multi-shard runs (and after adding post-review safeguard tests/code), the coordinator first merges every active shard's `last_green_sha` into the integration branch. Run this command from the integration checkout to verify ancestry, gate the integrated tree, and record its shared `code_tree_sha256`:
   `python tools/autoloop/cli.py check --run-id <id> --all-shards`
5. If the base branch (`main`) advances, reopen shards for fresh checks before resubmitting:
   `python tools/autoloop/cli.py refresh-base --run-id <id> --new-base-sha <sha>`
   When retargeting a stacked run, add `--new-base-branch main` and have the owner publish the revised contract. An owner can also use both flags on `resolve-contract-change`. For a local check after pre-integrating an out-of-scope dependency PR, pass its integration commit with `check --base-ref <integration-commit>`. PR CI uses the actual PR base.
6. On gate failure:
   - Record the failure via `retry-shard --run-id <id> --shard <shard_id> --check-name <gate> --error-output "<failure>"`. It normalizes the signature and preserves committed work by default. Max 3 worker strikes, max 1 replan, and max 8 total attempts apply. Add `--rollback` to explicitly restore the last green tree at the retry limit.
   - Log each dead-end hypothesis via `log-dead-end` before retrying.
   - Safe rollback refuses (`exit 4`) whenever the worktree has uncommitted or untracked edits anywhere (inside or outside `allowed_paths`).
   - If scope must change (including from `gate_passed`/`safeguard_pending`/`ready_to_merge`), call `request-contract-change` and wait for owner `resolve-contract-change` (monotonic `contract_revision`).
   - To add a shard, request it with `request-contract-change --run-id <id> --shard <new> --reason "<reason>" --proposed-path '<glob>'`. The owner runs `resolve-contract-change --run-id <id> --decision approve --add-shard-paths '<new>=<glob>' --shard-check '<new>=<command>'`, then publishes the new revision before starting that shard.
   - To remove an unused shard, run `request-contract-change --run-id <id> --shard <id> --drop --reason "<reason>"`. The owner runs `resolve-contract-change --run-id <id> --decision approve` and publishes the revised contract. A shard with findings or a registered worktree cannot be dropped. A started shard must also have no commits in its scope. Re-add former paths to an active shard through another approved amendment before changing them.
   - If strikes reach `needs_replan`, run `apply-replan`; if `blocked_human`, pause for `human-resolve`.

Record each delegated attempt with `record-attempt --task-id <id> --attempt 1 --task-class implementation --provider codex --model "GPT-6.2 Sol" --disposition accepted --checks-passed true`. Add difficulty, effort, speed, evidence ID, cost, review minutes, and elapsed time to acceptance when available. `routing-report` attributes first and eventual acceptance to the starting model, and spend to the model that incurred it. Routes with fewer than five initial attempts are flagged. Both commands accept `--log <external-path>`.

## Phase 4: Independent Review & Layered Safeguards

Register confirmed incidents, gate failures, and inherited review findings as soon as they are known.
`python tools/autoloop/cli.py record-finding --run-id <id> --finding-id <id> --severity P1 --source incident --summary "<bug>" --evidence "<log, review, or commit>" [--fixed-by <commit>]`
Use `--shard <id>` to associate a finding with its active shard and `--fixed-by` for a fix already committed. Review requires that commit in the gated tree. For an open finding, fix it and use `resolve-finding` before submission. A finding recorded during or after review reopens submission.

1. Once all shards reach `passed` at the same `code_tree_sha256` (`gate_passed`), run:
   `python tools/autoloop/cli.py submit-for-review --run-id <id>`
   (If both cloud and deep reviewers are explicitly disabled, this routes directly to `safeguard_pending` or `ready_to_merge` when there are no open oracle mismatches.)
2. Record review outcomes via `record-review`:
   - On `reject`, findings enter `open`. Fix them, call `resolve-finding` (`fixed_pending_review`), re-pass shard gates, and re-submit for review.
   - On `pass`, verified findings transition to `closed_needs_safeguard`.
3. Close every finding with a layered safeguard (`record-safeguard`) in strict priority order:
   1. `layer_1_architecture` (preferred: eliminate the failure class by design)
   2. `layer_2_type` (compile/schema/type boundary)
   3. `layer_3_static_lint` (AST/config static rule in `.autoloop/rules/`)
   4. `layer_4_runtime_test` (requires non-empty `--why-not-static` justification; 15-line prose cap)
   For layer 3, set `--artifact .autoloop/rules/<module>.py` and use `--rule-id <registered-id>` if it differs from the finding ID. Commit bad examples under `.autoloop/rules/fixtures/<rule-id>/<repo-relative-target>`. Check out the gated rule in the coordinator checkout. Registration runs that rule on the gated tree and then with the bad example substituted for its target. Record the safeguard again after any change to the rule module or its fixtures.
4. If a safeguard commit modifies code after review, run `check --run-id <id> --all-shards`, re-submit (`submit-for-review`), and re-record review on the new `code_tree_sha256` (existing finding/safeguard records are preserved).
5. Once `status == ready_to_merge`, verify with `verify-untampered` before opening/updating the PR. CI verifies `autoloop-preflight` -> (`autoloop-candidate-checks` + per-shard isolated `autoloop-shard-checks` matrix) -> `autoloop-integrity --mode finalize`, and `autoloop-comment-refresh.yml` re-runs the bound PR gate when owner contract/approval comments change.

Keep stock ERPNext and Frappe unchanged as the oracle. For a deliberate divergence caused by an upstream bug, run `record-oracle-mismatch --run-id <id> --shard <shard_id> --upstream-ref <file:line@version> --summary "<upstream bug>" --why-ours-is-correct "<reason>" --test <repo-relative-test-path>`. The owner publishes the revised FrozenContractV1. Commit the regression test and pass the gate. `submit-for-review` lists open mismatch records. An independent reviewer explicitly approves each one with `record-review --run-id <id> --stage <cloud-or-deep> --verdict pass --reviewer <name> --oracle-mismatch <id>` (repeat the flag for multiple IDs). If ordinary reviewers are disabled, use a named independent deep reviewer for the mismatch. Approvals must match the current base, contract revision, and gated code tree.

## Cross-Agent Skill Sync

Preview sync targets safely (dry-run default):
`python tools/autoloop/cli.py sync-skill --dry-run`

Install synced copies explicitly. Repository mirrors include Devin, Claude Code, and Agents.
`python tools/autoloop/cli.py sync-skill --install`
