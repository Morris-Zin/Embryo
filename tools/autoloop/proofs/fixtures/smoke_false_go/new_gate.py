"""Repaired qualification gate fixture: smoke evidence cannot qualify."""
import json
import sys
from pathlib import Path

payload = json.loads((Path(sys.argv[1]) / "result.json").read_text(encoding="utf-8"))
if payload.get("run_kind") == "smoke" and payload.get("verdict") == "GO" and payload.get("qualification_evidence") is True:
    print(json.dumps({"verdict": "NO-GO", "diagnostic": "smoke-run-cannot-qualify"}))
    raise SystemExit(2)
if payload.get("verdict") == "GO":
    print(json.dumps({"verdict": "GO", "diagnostic": ""}))
    raise SystemExit(0)
print(json.dumps({"verdict": "NO-GO", "diagnostic": "input-verdict-not-go"}))
raise SystemExit(2)
