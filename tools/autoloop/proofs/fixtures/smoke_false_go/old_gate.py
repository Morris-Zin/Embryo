"""Legacy qualification gate fixture: trusts any GO verdict."""
import json
import sys
from pathlib import Path

payload = json.loads((Path(sys.argv[1]) / "result.json").read_text(encoding="utf-8"))
if payload.get("verdict") == "GO":
    print(json.dumps({"verdict": "GO", "diagnostic": ""}))
    raise SystemExit(0)
print(json.dumps({"verdict": "NO-GO", "diagnostic": "input-verdict-not-go"}))
raise SystemExit(2)
