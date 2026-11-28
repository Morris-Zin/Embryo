"""Register the backend qualification findings in a coordinator's gated run."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

AUTOLOOP = Path(__file__).resolve().parents[1]
REPO = AUTOLOOP.parents[1]
sys.path.insert(0, str(AUTOLOOP.parent))

from autoloop.guard import git_rev_parse
from autoloop.state import load_run


BACKEND_RULES = ".autoloop/rules/backend_2gb.py"
STATIC = "layer_3_static_lint"

# Each row is ID, severity, source, fix, finding summary, safeguard summary.
FINDINGS = (
    (
        "fastpath-must-keep-upstream-validation", "P0", "inherited_review", "5c38bd7",
        "Counter sales skipped ERPNext's reserved-stock validation on the future SLE fast path.",
        "Require the registered upstream validator on every skipped-update exit.",
    ),
    (
        "stock-row-locks-only-in-stock-locks", "P1", "incident", "5c38bd7",
        "Late locking of bundle components could deadlock with other stock postings.",
        "Require one ordered Item/Bin acquisition boundary for all stock row locks.",
    ),
    (
        "no-chmod-on-operator-paths", "P1", "incident", "2c2db72",
        "Parity sidecar setup changed permissions on the shared /tmp directory.",
        "Constrain permission changes to the vetted cache and manifest paths.",
    ),
    (
        "report-columns-have-fieldname-or-key", "P1", "inherited_review", "2c2db72",
        "Financial comparison collapsed report columns with a label and no fieldname.",
        "Require label-or-position column keys when mapping positional report rows.",
    ),
    (
        "bench-results-typed-gated", "P1", "gate_failure", "2c2db72",
        "A gated Fast Gross Profit response was recorded as a successful latency sample.",
        "Classify Fast Gross Profit results before publishing successful timings.",
    ),
    (
        "wsl-scripts-lf", "P1", "gate_failure", None,
        "WSL failed to execute scripts checked out with CRLF line endings.",
        "Require Git LF attributes for shell scripts and WSL script entry points.",
    ),
    (
        "test-stubs-restored", "P1", "gate_failure", "4242c82",
        "Shared framework stub attributes leaked from one offline test suite into another.",
        "Reject new unscoped stub mutations using an exact-site legacy baseline.",
    ),
    (
        "lease-split-brain", "P1", "incident", "5dd94f0",
        "Windows and WSL workers could hold independent leases for the same resource.",
        "Use the shared Git lock directory and one Windows lock authority for Windows-backed WSL worktrees.",
    ),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--phase", choices=("findings", "safeguards"), required=True)
    parser.add_argument("--wsl-fix", help="Commit introducing the WSL LF Git attributes")
    args = parser.parse_args()
    if args.phase == "findings" and not args.wsl_fix:
        parser.error("--phase findings requires --wsl-fix")

    def run_cli(command: str, finding_id: str, *options: str) -> None:
        subprocess.run(
            [
                sys.executable, "-B", str(AUTOLOOP / "cli.py"), "--repo", str(REPO),
                command, "--run-id", args.run_id, "--finding-id", finding_id, *options,
            ],
            cwd=REPO,
            stdout=subprocess.DEVNULL,
            check=True,
        )
        print(f"{command} {finding_id}", flush=True)

    state = load_run(REPO, args.run_id)
    registered = state.get("findings", {})
    if args.phase == "findings":
        fixes = {fid: git_rev_parse(REPO, fix or args.wsl_fix) for fid, _, _, fix, _, _ in FINDINGS}
        for fid, severity, source, _, summary, _ in FINDINGS:
            if fid in registered:
                existing = registered[fid]
                if (existing.get("severity"), existing.get("source"), existing.get("fixed_by")) != (
                    severity, source, fixes[fid],
                ):
                    parser.error(f"finding {fid} already exists with different evidence")
                continue
            run_cli(
                "record-finding", fid, "--severity", severity, "--source", source, "--summary", summary,
                "--evidence", f"git commit {fixes[fid]}", "--fixed-by", fixes[fid],
            )
        return 0

    pending = [
        fid for fid, *_ in FINDINGS
        if registered.get(fid, {}).get("status") not in {"closed_needs_safeguard", "safeguarded"}
    ]
    if pending:
        parser.error(f"submit and complete review before recording safeguards: {', '.join(pending)}")
    for fid, _, _, _, _, summary in FINDINGS:
        if registered[fid]["status"] == "safeguarded":
            continue
        layer, artifact = STATIC, BACKEND_RULES
        if fid == "test-stubs-restored":
            artifact = ".autoloop/rules/stub_cleanup.py"
        elif fid == "lease-split-brain":
            layer, artifact = "layer_1_architecture", "tools/autoloop/leases.py"
        run_cli("record-safeguard", fid, "--layer", layer, "--artifact", artifact, "--summary", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
