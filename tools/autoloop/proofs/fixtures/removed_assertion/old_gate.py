"""Legacy evaluator fixture: trusts the runner's pass flag without assertion coverage."""
import json
import sys
from pathlib import Path

payload = json.loads((Path(sys.argv[1]) / "execution_report.json").read_text(encoding="utf-8"))
if payload.get("passed") is True:
    print(json.dumps({"verdict": "GO", "diagnostic": ""}))
    raise SystemExit(0)
print(json.dumps({"verdict": "NO-GO", "diagnostic": "test-run-failed"}))
raise SystemExit(2)
