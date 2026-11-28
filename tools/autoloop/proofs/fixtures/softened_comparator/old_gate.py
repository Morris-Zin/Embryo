"""Legacy comparator fixture: accepts gross_profit_% within a loose tolerance."""
import json
import math
import sys
from pathlib import Path

payload = json.loads((Path(sys.argv[1]) / "metrics.json").read_text(encoding="utf-8"))
actual = payload["actual"]["gross_profit_%"]
expected = payload["expected"]["gross_profit_%"]
if math.isclose(actual, expected, abs_tol=0.5):
    print(json.dumps({"verdict": "GO", "diagnostic": ""}))
    raise SystemExit(0)
print(json.dumps({"verdict": "NO-GO", "diagnostic": "gross-profit-percent-mismatch"}))
raise SystemExit(2)
