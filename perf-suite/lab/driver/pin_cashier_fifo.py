"""Install the immutable v3 cashier harness with an additional read-only SLE audit."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


OPERATOR = Path("/srv/khitmee-lab/operator")
REFERENCE = "cashier-screen-v3"
REFERENCE_PIN = "66431d1aebeb58a7beb447a7f717e567861a612aae5071c22be7543912d42c58"
AUDIT_SHA = "61a76b5ac38509ca11721ea57ce9109965f5e41d67a5893498e9e5b4234897bd"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def install(name):
    if os.geteuid() or not re.fullmatch(r"cashier-fifo-v[1-9][0-9]*", name):
        raise ValueError("run as the WSL operator administrator with a fresh cashier-fifo version")
    source = OPERATOR / "releases" / REFERENCE
    release = OPERATOR / "releases" / name
    store = OPERATOR / "stores" / name
    pin = OPERATOR / "pins" / name
    if any(p.exists() for p in (release, store, pin)):
        raise ValueError("release already exists")
    source_config = (OPERATOR/"stores"/REFERENCE/"objects"/REFERENCE_PIN).read_bytes()
    if sha(source_config) != REFERENCE_PIN or (OPERATOR/"pins"/REFERENCE).read_text().strip() != REFERENCE_PIN:
        raise ValueError("historical release pin mismatch")
    cfg = json.loads(source_config)
    sys.path.insert(0, str(source))
    import experiments
    if experiments.harness_hash(source / "harness") != cfg["harness_sha256"]:
        raise ValueError("source harness does not match immutable pin")
    payload = Path(__file__).with_name("cashier_fifo_observer.py").read_bytes().replace(b"\r\n", b"\n")
    audit = Path("harness/perf-suite/lab/seasonal-smoke/audit.py")
    old = (source/audit).read_bytes()
    if sha(old) != AUDIT_SHA:
        raise ValueError("unsupported historical cashier audit")
    original = b'print(json.dumps({"counts": totals,'
    if old.count(original) != 1:
        raise ValueError("audit boundary missing or ambiguous")
    augmented = payload + b"\n" + old.replace(original, b'    stock_trace = capture_fifo_trace(cur)\nprint(json.dumps({"fifo_trace":stock_trace, "counts": totals,')
    compile(augmented, "cashier_fifo_audit.py", "exec")
    shutil.copytree(source, release)
    (release/audit).write_bytes(augmented)
    harness = release / "harness"
    adapter = harness / "perf-suite/lab/environment/khitmee_lab_adapter.py"
    settings = adapter.with_name("lab-environment.json")
    data = json.loads(settings.read_bytes())
    driver = harness / "perf-suite/lab/driver/seasonal_driver.py"
    if sha(driver.read_bytes()) != data["harness"]["sha256"]:
        raise ValueError("copied driver differs")
    data["harness"]["executable"] = str(driver)
    settings.write_text(json.dumps(data, indent=2) + "\n")
    receipt = {"reference_release":REFERENCE, "reference_manifest_sha256":REFERENCE_PIN,
               "original_audit_sha256":AUDIT_SHA, "observer_sha256":sha(payload),
               "instrumented_audit_sha256":sha(augmented),
               "application":cfg["baseline_commit"], "fixture_sha256":cfg["fixture_sha256"]}
    (release/"fifo-pin.json").write_text(json.dumps(receipt, indent=2)+"\n")
    for path in [release,*release.rglob("*")]:
        shutil.chown(path, "root", "km-lab-operator")
        path.chmod(0o550 if path.is_dir() or path in (driver, adapter) else 0o440)
    store.mkdir(mode=0o700)
    shutil.chown(store, "km-lab-operator", "km-lab-operator")
    command = ["runuser", "-u", "km-lab-operator", "--", "python3", "-B",
               str(release/"experiments.py"), "--store", str(store)]
    subprocess.run(command+["pin", "--repo", cfg["repo"], "--commit", cfg["baseline_commit"],
                            "--adapter", str(adapter), "--harness", str(harness), "--fixture", cfg["fixture_sha256"],
                            "--workload", cfg["workload"], "--screen-latency", cfg["screen_latency"],
                            "--min-improvement-pct", str(cfg["min_improvement_pct"]), "--pairs", str(cfg["pairs"])], check=True)
    pin.write_bytes((store/"BASELINE").read_bytes())
    shutil.chown(pin, "root", "km-lab-operator")
    pin.chmod(0o440)
    key = pin.read_text().strip()
    pinned = json.loads((store/"objects"/key).read_bytes())
    subprocess.run(["chattr", "+i", str(pin), str(store/"BASELINE"), str(store/"objects"/key),
                    str(store/"objects"/pinned["source_tar"])], check=True)
    subprocess.run(command+["verify"],check=True)
    print(json.dumps({**receipt, "manifest_sha256":key, "harness_sha256":pinned["harness_sha256"]}))


if __name__ == "__main__":
    install(sys.argv[1])
