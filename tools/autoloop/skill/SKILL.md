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
2. Run a focused `/grill-me` + `/grill-with-docs` interview to resolve ambiguity, edge cases, non-goals, acceptance tests, and domain vocabulary.
3. Update `GLOSSARY.md` and `docs/adr/` lazily when new domain terms or architectural decisions are resolved during the interview.
4. Confirm the user's preferred **model roster** (defaulting to `docs/agents/routing.md` presets in `.autoloop/config.json`) and reviewer configuration (`cloud`, `deep`, or explicitly disabled).
5. Confirm whether merge mode is `human` (default) or opt-in `auto` (which still requires owner-applied `allow-auto-merge` label + approval comment).

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

1. Acquire resource leases before touching shared resources (use `--holder-pid <pid>` to bind to a long-lived worker PID, or rely on TTL + secret `--token`; `--token` is mandatory on `renew` and `release`):
   `python tools/autoloop/cli.py lease acquire --resource <key> --owner <worker> [--holder-pid <pid>]`
   `python tools/autoloop/cli.py lease release --resource <key> --owner <worker> --token <lease_token>`
2. Start each shard (`start-shard` supports concurrent multi-shard execution while `run.status` is `locked` or `in_progress`):
   `python tools/autoloop/cli.py start-shard --run-id <id> --shard <shard_id> --worktree <path>`
3. Commit changes cleanly inside the shard worktree, then verify shard-local boundaries, static rules, and tests (`check` inspects the clean registered shard worktree automatically and refuses dirty/mismatched checkouts):
   `python tools/autoloop/cli.py check --run-id <id> --shard <shard_id>`
4. For multi-shard runs (and after adding post-review safeguard tests/code), merge shard branches into the integration branch and record the shared integrated `code_tree_sha256`:
   `python tools/autoloop/cli.py check --run-id <id> --all-shards`
5. If the base branch (`main`) advances, reopen shards for fresh checks before resubmitting:
   `python tools/autoloop/cli.py refresh-base --run-id <id> --new-base-sha <sha>`
6. On gate failure:
   - Record the failure via `retry-shard` (deterministic signature normalization; max 3 worker strikes, max 1 replan, max 8 total attempts; safe rollback restores `last_green_sha` when worktree is clean and inside shard scope).
   - Log each dead-end hypothesis via `log-dead-end` before retrying.
   - Safe rollback refuses (`exit 4`) whenever the worktree has uncommitted or untracked edits anywhere (inside or outside `allowed_paths`).
   - If scope must change (including from `gate_passed`/`safeguard_pending`/`ready_to_merge`), call `request-contract-change` and wait for owner `resolve-contract-change` (monotonic `contract_revision`).
   - If strikes reach `needs_replan`, run `apply-replan`; if `blocked_human`, pause for `human-resolve`.

## Phase 4: Independent Review & Layered Safeguards

1. Once all shards reach `passed` at the same `code_tree_sha256` (`gate_passed`), run:
   `python tools/autoloop/cli.py submit-for-review --run-id <id>`
   (If both cloud and deep reviewers are explicitly disabled in `FrozenContractV1`, this routes directly to `safeguard_pending` or `ready_to_merge`.)
2. Record review outcomes via `record-review`:
   - On `reject`, findings enter `open`. Fix them, call `resolve-finding` (`fixed_pending_review`), re-pass shard gates, and re-submit for review.
   - On `pass`, verified findings transition to `closed_needs_safeguard`.
3. Close every finding with a layered safeguard (`record-safeguard`) in strict priority order:
   1. `layer_1_architecture` (preferred: eliminate the failure class by design)
   2. `layer_2_type` (compile/schema/type boundary)
   3. `layer_3_static_lint` (AST/config static rule in `.autoloop/rules/`)
   4. `layer_4_runtime_test` (requires non-empty `--why-not-static` justification; 15-line prose cap)
4. If a safeguard commit modifies code after review, run `check --run-id <id> --all-shards`, re-submit (`submit-for-review`), and re-record review on the new `code_tree_sha256` (existing finding/safeguard records are preserved).
5. Once `status == ready_to_merge`, verify with `verify-untampered` before opening/updating the PR. CI verifies `autoloop-preflight` -> (`autoloop-candidate-checks` + per-shard isolated `autoloop-shard-checks` matrix) -> `autoloop-integrity --mode finalize`, and `autoloop-comment-refresh.yml` re-runs the bound PR gate when owner contract/approval comments change.

## Cross-Agent Skill Sync

Preview sync targets safely (dry-run default):
`python tools/autoloop/cli.py sync-skill --dry-run`

Install synced copies explicitly:
`python tools/autoloop/cli.py sync-skill --install`
