from __future__ import annotations

from pathlib import Path
from typing import Optional

from .claude import read_claude_transcripts
from .codex import read_codex_rollouts
from .devin import scan_devin_cli_logs
from .model import ScanResult
from .provider_logs import read_t3_provider_logs
from .reconcile import filter_project, reconcile_scan
from .t3_state import read_t3_state


def collect_scan(
    *,
    since: str,
    project_filter: str | None,
    t3_db: object,
    codex_home: object,
    claude_project_dir: object,
    t3_provider_log_dir: object,
    devin_log_dir: object,
) -> ScanResult:
    scan = ScanResult()
    scan.extend(read_t3_state(t3_db, since, project_filter))
    scan.extend(read_codex_rollouts(codex_home, since, project_filter))
    scan.extend(read_claude_transcripts(claude_project_dir, since, project_filter))
    scan.extend(read_t3_provider_logs(t3_provider_log_dir, since))
    scan.extend(scan_devin_cli_logs(devin_log_dir, since))
    reconcile_scan(scan)
    filter_project(scan, project_filter)
    return scan


def default_paths(home: object | None = None) -> dict[str, Path]:
    base = Path(str(home)).expanduser() if home is not None else Path.home()
    return {
        "t3_db": base / ".t3" / "userdata" / "statev2.sqlite",
        "codex_home": base / ".codex",
        "claude_project_dir": base / ".claude" / "projects" / "E--Khitmee-ERPNext",
        "t3_provider_log_dir": base / ".t3" / "userdata" / "logs" / "provider",
        "devin_log_dir": base / "AppData" / "Roaming" / "devin" / "cli" / "logs",
    }
