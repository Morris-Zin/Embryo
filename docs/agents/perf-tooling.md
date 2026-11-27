# Perf tooling notes

Docker runs in the KhitmeeERPNext WSL distribution. The Windows wrapper preserves quoted SQL, JSON filters and multiline Python as UTF-8. In Bash, pipe a heredoc to `py -` or `sql -`. When writing a discovery loop yourself, give noninteractive Docker calls `< /dev/null` so they cannot consume the next iteration's input.

SQL uses the installed site's account on the perf Compose network. Narrow queries with LIMIT or aggregates. Display stops at 40 rows. The site Python runner starts in the bench sites directory. Installed upstream paths start at the bench apps directory, for example `erpnext/erpnext/accounts/report/gross_profit/gross_profit.py`.

A prepared report can return quickly while queuing work. The HTTP probe requests synchronous execution and requires a result array. Choose a narrow date range for Gross Profit. Timing excludes login and includes reading the response body. Compare runs on the same dataset and distinguish cold and warm runs.

The offline gate parses checked-out shell sources with Git-style LF normalization and enforces LF on tools scripts. It syntax-checks app and k6 JS without running either application. Knip uses the app manifest's version. With Bun installed in KhitmeeERPNext, warm its external cache once from the app directory with `bun x --bun knip@6.46.1 --version`, then run `bash tools/khitmee-dev.sh fast-check` from the repository root. For Node, warm the matching npx cache with `npx --yes knip@6.46.1 --version`.

Python verification uses the pinned `unittest-parallel` runner. Financial, seeder, k6 and offload suites keep their original sequential discovery because some legacy modules share import-time Frappe test doubles; the independent tools suite uses class-level parallelism because it dominates the offline gate. Do not introduce cross-class coupling in tools tests without re-measuring and adjusting `TOOLS_TEST_PARALLEL_WORKERS`. The gate also runs pinned `zizmor` over GitHub workflows; suppressions belong in `.github/zizmor.yml` and require a concrete security justification.
