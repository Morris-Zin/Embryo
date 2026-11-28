from __future__ import annotations

import argparse
import json
import sys

from .pipeline import collect_scan, default_paths
from .report import build_report, render_markdown


def build_parser() -> argparse.ArgumentParser:
    defaults = default_paths()
    parser = argparse.ArgumentParser(description="Profile existing local agent usage records without modifying them.")
    parser.add_argument("--since", default="2026-11-28", help="ISO date or timestamp lower bound (default: 2026-11-28)")
    parser.add_argument("--project", default="Khitmee-ERPNext", help="project path/title filter (default: Khitmee-ERPNext)")
    parser.add_argument("--all-projects", action="store_true", help="disable project filtering")
    parser.add_argument("--t3-db", default=str(defaults["t3_db"]))
    parser.add_argument("--codex-home", default=str(defaults["codex_home"]))
    parser.add_argument("--claude-project-dir", default=str(defaults["claude_project_dir"]))
    parser.add_argument("--t3-provider-log-dir", default=str(defaults["t3_provider_log_dir"]))
    parser.add_argument("--devin-log-dir", default=str(defaults["devin_log_dir"]))
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--top", type=int, default=10)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_filter = None if args.all_projects else args.project
    scan = collect_scan(
        since=args.since,
        project_filter=project_filter,
        t3_db=args.t3_db,
        codex_home=args.codex_home,
        claude_project_dir=args.claude_project_dir,
        t3_provider_log_dir=args.t3_provider_log_dir,
        devin_log_dir=args.devin_log_dir,
    )
    report = build_report(scan, args.since, project_filter)
    if args.format == "json":
        json.dump(report, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_markdown(report, top=args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
