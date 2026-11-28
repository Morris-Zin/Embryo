# Sales-voucher journey generation investigation

Date: 2026-11-27

## Verdict

GraphWalker and Hypothesis are both viable as sequence generators, but neither is an oracle and neither can prove parity by itself. The existing Playwright/Slint MCP differential harness must remain the authoritative executor because it applies the same action sequence to web and native and compares observable state plus ERP readback after every step.

## GraphWalker

Fit: model the voucher as explicit states (`empty`, `line-draft`, `search-open`, `customer-selected`, `submitting`, `submitted`, `validation-error`) and edges (`scan exact`, `search ambiguous`, `choose result`, `increase`, `decrease`, `quantity commit`, `clear`, `select customer`, `F2`, `F9`, `offline/error`). GraphWalker can generate deterministic offline paths with coverage-oriented stop conditions, which is useful for systematic edge coverage.

Cost/risk: introduces a Java/GraphML modeling toolchain and a second source of truth. If the model omits a browser behavior, generated paths still inherit that blind spot. It should consume the same JSON action schema already accepted by `tools/differential_journey.mjs` and must not replace browser-derived acceptance criteria.

Recommended use: later, for coverage expansion after the hand-written critical-path corpus passes.

## Hypothesis

Fit: Hypothesis stateful testing can generate pseudo-random action sequences and shrink failures to shorter counterexamples. A Python `RuleBasedStateMachine` could either execute a Python port of the harness directly or emit JSON sequences consumed by the existing Node harness. Shrinking is valuable when a long scroll/search/quantity sequence exposes a defect.

Cost/risk: Hypothesis runs Python while the current executor is Node because Playwright and Slint MCP orchestration are already implemented there. Naively generating UI actions can create many impossible or dominated sequences; the model needs precondition rules and action weights. ERP-mutating journeys also require unique fixture data or idempotency isolation so generated submissions do not corrupt comparisons.

Recommended use: implement generation behind a deterministic seed once the critical-path corpus is stable. Store every generated sequence and failing shrunk sequence as reproducible evidence.

## Near-term acceptance rule

Before adopting either generator, the fixed corpus must cover: exact barcode scan, ambiguous search and choose, validation failures, customer/warehouse/payment selection, F2/F9 keyboard paths, quantity commit semantics, increase/decrease/remove/clear, scrolling/clipping, submit success, submit permission failure, ERP/network failure, and idempotent replay. Generated journeys are an expansion mechanism, not a substitute for these required comparisons.
