## Agent skills

### Issue tracker

Issues and specs live as GitHub issues in this repo, managed with the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles use their default label strings. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout: `GLOSSARY.md` and ADRs under `docs/adr/` at the repo root. See `docs/agents/domain.md`.

### Autonomous loop

Never auto-invoke `/autoloop` on ordinary prompts; activate it **only** when the user explicitly invokes `/autoloop` or names an active `.autoloop/runs/<id>.json`.

For perf probes and local CI, use `khitmee-perf-ops`. For delegated model selection, see `docs/agents/routing.md`.
