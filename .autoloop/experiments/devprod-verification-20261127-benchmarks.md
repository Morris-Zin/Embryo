# Developer productivity, CI/CD performance, and verification engineering

Run: `devprod-verification-20261127`  
Base: `78666698e2ec4b9b6835414dcb79f6d46c8557fe` (`main` at run start)

## Research summary and applicability

| Source | Actionable finding | Embryo decision |
| --- | --- | --- |
| Anthropic/Claude Code, [How the Claude Code team thinks about verification](https://claude.dev/blog/how-the-claude-code-team-thinks-about-verification/) | Verification loops let agents self-check without consuming review tokens; slow/flaky tests teach bad retry behavior. Invest in end-to-end, property, and differential tests that produce actionable evidence. | Retained existing ERPNext oracle differential tests, Hypothesis property tests, browser E2E checks, and deterministic fast gates. Optimized the fast gate instead of adding another slow mandatory layer. |
| Anthropic, [scaling test impact analysis](https://claude.com/resources/articles/agentic-coding-is-straining-ci-heres-how-we-scaled-test-impact-analysis-at-anthropic) | Agent-generated code volume makes naive run-everything CI a bottleneck; deterministic test selection needs trustworthy listener/selector infrastructure. | Not implemented: Embryo's offline gate is only ~8s, so a custom impact-analysis service would cost more complexity and trust than it saves. Revisit when the gate exceeds its 60s budget materially. |
| Lauren Tan's X talk, [How I shipped 2,500 PRs last month](https://x.com/poteto/status/2102050467505430555/video/1), and [transcript mirror](https://2outube.com/watch?v=Z-jNqqIYGm4) | Trust before parallel agents; encode recurring agent mistakes in codebase rules, static checks, and executable verification. | Converted recurring GitHub Actions security findings into a pinned zizmor gate instead of prose-only review guidance. |
| Matt Pocock, [TDD skill](https://github.com/mattpocock/skills/blob/main/docs/engineering/tdd.md) and [agent self-check guidance](https://www.linkedin.com/posts/mapocock_whenever-youre-working-with-an-ai-agent-activity-7308525035994923008-2TQ0) | Give agents fast feedback they can run themselves; test at public seams and avoid tautological tests. | Kept the same unittest seams and count; parallelism changes execution scheduling, not assertions. Added positive/negative tests for the runner selection and dev-tool gate. |
| [`unittest-parallel`](https://pypi.org/project/unittest-parallel/) | Existing unittest-compatible runner supports module/class/test granularity. Class/test levels can violate module or class fixture assumptions. | Adopted pinned `unittest-parallel==1.8.6`; financial/seeder suites keep sequential discovery, while the dominant tools suite uses measured class-level parallelism. Test-level parallelism was rejected despite being faster because it weakens fixture semantics. |
| [zizmor](https://pypi.org/project/zizmor/) and [configuration docs](https://docs.zizmor.sh/configuration/) | Existing GitHub Actions static analyzer detects template injection, unpinned actions, credential persistence, dangerous triggers, and related CI/CD defects. | Adopted pinned `zizmor==1.33.1`; runtime measured at ~0.15-0.16s for all workflows. |
| GitHub [dependency caching reference](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching), [`setup-python` cache docs](https://github.com/actions/setup-python/), [`setup-node` cache docs](https://github.com/actions/setup-node) | Dependency caches are safe when keyed by dependency manifests; they avoid repeated network/package resolution without skipping verification. | Added pip/npm caching to offline candidate jobs. Tests still execute fully on every run. |
| [Python mutation-testing comparison](https://dl.acm.org/doi/full/10.1145/3701625.3701659) and [mutmut removal ADR example](https://mhallum.github.io/calista/dev/adr/0017-remove-mutation-testing-from-ci/) | Mutation testing gives strong test-effectiveness signal but can be expensive and operationally fragile as a mandatory gate. | Not added to mandatory CI. A targeted injected-defect check demonstrated the new zizmor gate instead. Reconsider diff-scoped mutation testing only after the faster gate is stable. |
| ast-grep, Semgrep, Ruff, Clippy, dependency-boundary enforcement | Ruff is already present; Embryo's financial/package-boundary rules are Python-AST-specific and already have positive/negative tests. Clippy is irrelevant because the native Rust workspace is out of scope. Semgrep/ast-grep would duplicate existing rules or require another runtime. | No additional adoption in this focused PR. Zizmor was the only new static analyzer because it covers a distinct workflow-security defect class with measured sub-second cost. |

## Measured baseline and bottleneck ranking

Controlled local baseline used a clean `main` worktree on this VM, Python 3.12.16, warm dependency/tool caches:

| Rank | Bottleneck | Evidence | Impact |
| --- | --- | --- | --- |
| 1 | Serial Python unittest discovery | `tools` suite alone: 6.02s, 6.23s, 6.47s in repeated sequential runs; full fast-check controlled baseline: 8.48s and 7.41s | Python tests formed the critical path; every other fast-check job finished earlier. |
| 2 | CI dependency/setup work | GitHub main CI run `46034217934`: 30s job wall; checkout/setup 13s, pip install 5s, knip warm 4s, fast-check 7s | Setup was ~63% of the measured CI job before the actual gate. |
| 3 | Duplicate offline fast-check for enrolled PRs | Ordinary CI and `autoloop-candidate-checks` both execute `python tools/khitmee_dev.py fast-check` for an enrolled PR | No extra defect signal; doubles runner compute for enrolled PRs. |
| 4 | Missing workflow-security static analysis | `zizmor --collect=workflows .` on baseline reported 19 findings in 0.16s: 16 unpinned action references, 2 credential-persistence findings, 1 intentional `issue_comment` dangerous-trigger finding | High defect-detection value at negligible runtime cost. |
| 5 | Local Python version/bootstrap mismatch | VM `python` is 3.10.12; autoloop rules require `tomllib` (3.11+) and project metadata requests 3.14. Using pyenv Python 3.12.16 manually unblocked verification | Environment/blueprint issue; not hidden by weakening checks. |

## Controlled before/after result

Same VM, same dependency/tool caches, `python -B tools/khitmee_dev.py fast-check`:

| Metric | Before (`main` worktree) | After (this branch) | Change |
| --- | ---: | ---: | ---: |
| Wall time, controlled runs | 8.48s, 7.41s (mean 7.95s) | 2.81s, 2.65s, 2.86s (mean 2.77s) | **65.1% faster** |
| fast-check self-reported elapsed | 8.36s, 7.32s | 2.68s, 2.57s, 2.79s | consistent with wall timing |
| CPU utilization during gate | 122-123% | 264-275% | higher parallelism during a much shorter window |
| Approximate CPU-seconds | mean ~9.73 CPU-s | mean ~7.51 CPU-s | **22.9% less CPU time** |
| Peak RSS | 95-98 MB | 94-98 MB | no material increase |
| Test count | unchanged | unchanged | every suite still runs and must discover nonzero tests |

The first uncontrolled pre-change run in the working checkout was 9.46s elapsed; the controlled clean-worktree numbers above are the comparison used for the improvement claim.

## Implemented improvements

1. **Parallel unittest execution with preserved semantics.** `tools/fast_check.py` uses pinned `unittest-parallel` only for the independent tools suite. Financial, seeder, k6, and offload suites keep their original sequential discovery because they contain legacy import-time Frappe test-double coupling. That legacy serial stream overlaps the dominant tools suite, which runs at class level with four workers after measured validation.
2. **Workflow security gate.** `zizmor` runs in fast-check. Actions are pinned to commit SHAs at the same major versions already in use, and checkout credential persistence is disabled where workflows do not push.
3. **CI dependency caching and setup reduction.** Python and npm caches are enabled in CI/autoloop candidate jobs using dependency-manifest keys. Redundant `pip install --upgrade pip` was removed because setup-python's Python 3.14 installation already provides a pip new enough for `--group` installs.
4. **Stale-check elimination.** CI uses per-PR/per-ref concurrency cancellation so superseded pushes do not keep consuming runners. An attempted labeled-PR fast-check skip was reverted after review because ordinary CI tests the merge result while candidate checks intentionally test the immutable PR head; those are not redundant verification signals.
5. **Knowledge capture.** `docs/agents/perf-tooling.md` records the suite-parallelism constraint and zizmor suppression policy.

## Deliberate defect detection evidence

A temporary workflow containing the historical defect class from PR #12:

```yaml
- run: echo "${{ inputs.branch }}"
```

was inserted locally, scanned, then deleted. Result: zizmor exited nonzero and reported `[template-injection]: code injection via template expansion` at the injected line. The final explicitly offline gate reproduced the detection with exit `13`, while the same scan exits `0` on the fixed workflow set even with `GH_TOKEN` exported. This is direct evidence that the new gate detects the previous shell-injection class rather than merely adding a tool.

## Rejected or unsuccessful experiments

- **Class-level parallelism for every suite:** rejected. `apps/khitmee_ops` produced 9 errors because legacy classes share import-time Frappe test doubles; all 232 tests still ran, but the result correctly failed.
- **Module-level parallelism for legacy suites:** rejected after an initially lucky pass. A repeat fast-check failed nondeterministically with 24 errors and 2 failures because separate app test modules raced shared Frappe doubles. Those suites retain exact sequential discovery.
- **Module-level parallelism for tools:** correct but slower. The tools suite measured about 4.54s versus about 2.5-3.7s for class-level parallelism in repeated runs.
- **Test-level parallelism:** fastest in one experiment (~1.61s for tools) but rejected because unittest-parallel documents weaker fixture semantics at that level; speed does not justify weakening verification assumptions.
- **Skipping ordinary CI on labeled autoloop PRs:** rejected after review. It reduced duplicate runtime, but ordinary CI tests the merge result while autoloop candidate checks test the immutable head; skipping the former would miss merge-only failures. An earlier GitHub object-filter condition was also rejected because zizmor could not parse it and suppressed workflow-expression audits.
- **Mandatory mutation testing:** deferred, not implemented. Literature and prior project experience show high value but high runtime/operational cost; adding it to a sub-10s gate would violate the measured-impact priority.

## Remaining ranked opportunities

1. Measure more warm-cache CI samples before making stronger CI-duration claims; the first PR sample completed in about 26s versus 30s for baseline run `46034217934`, with fast-check itself at 4.20s versus the baseline 7s step.
2. If the fast-check grows materially, add deterministic test-impact analysis using recorded test results rather than ad-hoc path rules.
3. Evaluate diff-scoped mutmut as an optional nightly/nonblocking signal before considering it mandatory.
4. Fix the environment blueprint so future VMs bootstrap a compatible Python instead of requiring a manually selected pyenv interpreter.
