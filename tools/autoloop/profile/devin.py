from __future__ import annotations

import gzip
from pathlib import Path

from .model import ScanNotice, ScanResult


def scan_devin_cli_logs(log_dir: object, since: str) -> ScanResult:
    result = ScanResult()
    provider = "devin"
    result.providers.add(provider)
    root = Path(str(log_dir)).expanduser()
    if not root.exists():
        result.notices.append(ScanNotice(provider, "devin_cli_log", f"Devin CLI log directory not found: {root}", "warning"))
        return result
    since_compact = since[:10].replace("-", "")
    files = sorted(path for path in root.glob("devin_*.log*") if path.is_file() and path.name >= f"devin_{since_compact}")
    structured = 0
    scanned = 0
    for path in files:
        scanned += 1
        try:
            if path.suffix == ".gz":
                with gzip.open(path, "rb") as handle:
                    data = handle.read(2_000_000)
            else:
                with path.open("rb") as handle:
                    data = handle.read(2_000_000)
        except OSError:
            continue
        lowered = data.lower()
        if b"input_tokens" in lowered or b"output_tokens" in lowered or b"cache_read_input_tokens" in lowered or b"costusd" in lowered:
            structured += 1
    if structured:
        result.notices.append(ScanNotice(provider, "devin_cli_log", f"{structured} Devin CLI logs contain token-like structured keys; no parser is implemented", "warning"))
    else:
        result.notices.append(ScanNotice(provider, "devin_cli_log", f"Scanned {scanned} Devin CLI logs; no structured token, usage, reasoning, or cost counters found", "warning"))
    return result
