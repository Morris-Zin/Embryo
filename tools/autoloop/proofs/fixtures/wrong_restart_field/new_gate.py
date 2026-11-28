"""Repaired restart gate fixture: reads the top-level RestartCount field."""
import json
import sys
from pathlib import Path

payload = json.loads((Path(sys.argv[1]) / "inspect.json").read_text(encoding="utf-8"))
restart_count = payload.get("RestartCount")
if restart_count == 0:
    print(json.dumps({"verdict": "GO", "diagnostic": ""}))
    raise SystemExit(0)
print(json.dumps({"verdict": "NO-GO", "diagnostic": "unexpected-restart-count"}))
raise SystemExit(2)
