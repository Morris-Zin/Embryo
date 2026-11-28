"""Benign fixture gate: both versions implement the same small oracle."""
import json
import sys
from pathlib import Path

payload = json.loads((Path(sys.argv[1]) / "value.json").read_text(encoding="utf-8"))
if payload.get("value") == 10:
    print(json.dumps({"verdict": "GO", "diagnostic": ""}))
    raise SystemExit(0)
print(json.dumps({"verdict": "NO-GO", "diagnostic": "value-mismatch"}))
raise SystemExit(2)
