"""Repaired evaluator fixture: requires every mandatory assertion to execute."""
import json
import sys
from pathlib import Path

payload = json.loads((Path(sys.argv[1]) / "execution_report.json").read_text(encoding="utf-8"))
required = set(payload.get("required_assertions", ()))
completed = set(payload.get("completed_assertions", ()))
missing = sorted(required - completed)
if missing:
    print(json.dumps({"verdict": "NO-GO", "diagnostic": "missing-assertion:" + missing[0]}))
    raise SystemExit(2)
if payload.get("passed") is True:
    print(json.dumps({"verdict": "GO", "diagnostic": ""}))
    raise SystemExit(0)
print(json.dumps({"verdict": "NO-GO", "diagnostic": "test-run-failed"}))
raise SystemExit(2)
