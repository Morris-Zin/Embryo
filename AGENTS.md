## Agent skills

### Issue tracker

Issues and specs live as GitHub issues in this repo, managed with the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles use their default label strings. See `docs/agents/triage-labels.md`.


### Guardrails and CI

- Domain vocabulary: [`GLOSSARY.md`](GLOSSARY.md)
- Architecture decision: [ADR-0001](docs/adr/0001-daily-profit-summary-and-erpnext-oracle.md)
- Mechanical offline guardrails: `tools/fast_check.py` (`python tools/khitmee_dev.py fast-check`). This gate is honestly offline and never proves live bench oracle parity; the required live gate is `python tools/perf_parity.py --require-db`.

### Domain docs

Single-context layout: `GLOSSARY.md` and ADRs under `docs/adr/` at the repo root. See `docs/agents/domain.md`.

### Autonomous loop

Never auto-invoke `/autoloop` on ordinary prompts; activate it **only** when the user explicitly invokes `/autoloop` or names an active `.autoloop/runs/<id>.json`.

For perf probes and local CI, use `khitmee-perf-ops`. For delegated model selection, see `docs/agents/routing.md`.
