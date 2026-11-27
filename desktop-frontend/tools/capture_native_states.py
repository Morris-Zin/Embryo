#!/usr/bin/env python3
"""Capture native states in isolated application instances.

Keyboard probes intentionally run after visual/structural capture and mutate the
application. This orchestrator therefore starts a fresh native process for every state so
one state's probe cannot contaminate another state's evidence.
"""
from __future__ import annotations

import json
import os
import subprocess
import socket
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
EXE = ROOT / "target" / "debug" / "khitmee-desktop.exe"

F2 = {"type":"probe-key","key":"F2","probeText":"KM","targetElementId":"item-search"}
F9_EMPTY = {"type":"probe-key","key":"F9","expectText":"Add at least one item before submitting."}
F9_BLOCKED = {"type":"probe-key","key":"F9","expectText":"BLOCKED: authoritative ERPNext submission"}

def fill_search(value):
    return [{"type":"fill","elementId":"item-search","value":value},{"type":"wait","ms":500}]

def states():
    search = fill_search("Mekong") + [F2, F9_EMPTY]
    draft = fill_search("8850000000010") + [{"type":"key","key":"Enter"},{"type":"wait","ms":500},F2,F9_BLOCKED]
    return {
        ("sales-voucher","initial"): {"actions":[F2,F9_EMPTY]},
        ("sales-voucher","search-results"): {"actions":search},
        ("sales-voucher","draft-line"): {"actions":draft},
        ("item-search","initial"): {"initial":"item-search","actions":[F2,F9_EMPTY]},
        ("stock-transfer","initial"): {"actions":[{"type":"click","label":"Transfer"},F2,F9_EMPTY]},
        ("stock-transfer","search-results"): {"actions":[{"type":"click","label":"Transfer"}]+search},
        ("stock-transfer","draft-line"): {"actions":[{"type":"click","label":"Transfer"}]+draft},
        ("invoice-list","initial"): {"initial":"invoice-list","actions":[]},
        ("invoice-list","filtered"): {"initial":"invoice-list","actions":[{"type":"fill","elementId":"invoice-filter","value":"04921"},{"type":"wait","ms":500}]},
    }

def ready(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=.2):
            return True
    except OSError:
        return False

def main():
    if not EXE.exists():
        print(f"missing executable {EXE}; build khitmee-desktop first", file=sys.stderr)
        return 2
    failures=[]
    for index,((screen,state),config) in enumerate(states().items()):
        port=49400+index
        env=os.environ.copy()
        env.update({"SLINT_MCP_PORT":str(port),"SLINT_EMIT_DEBUG_INFO":"1","KHITMEE_CATALOG_ROWS":"10000","KHITMEE_INVOICE_ROWS":"10000"})
        if config.get("initial"): env["KHITMEE_INITIAL_SCREEN"]=config["initial"]
        subprocess.run(["taskkill.exe","/F","/IM","khitmee-desktop.exe"],capture_output=True)
        time.sleep(0.35)
        process=subprocess.Popen([str(EXE)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            deadline=time.time()+35
            while time.time()<deadline and not ready(port): time.sleep(.05)
            if not ready(port):
                failures.append((screen,state,"native MCP did not start")); continue
            time.sleep(1.25)
            cmd=["node",str(ROOT/"tools"/"capture_native_mcp.mjs"),"--port",str(port),"--screen",screen,"--state",state,"--actions",json.dumps(config["actions"],separators=(",",":"))]
            result=subprocess.run(cmd,cwd=REPO,capture_output=True,text=True,timeout=30)
            if result.returncode:
                failures.append((screen,state,(result.stderr+result.stdout).strip()[-500:]))
        finally:
            subprocess.run(["taskkill.exe","/F","/IM","khitmee-desktop.exe"],capture_output=True)
            time.sleep(0.2)
            try: process.kill(); process.wait(timeout=2)
            except Exception: pass
    print(json.dumps({"passed":not failures,"failures":failures},indent=2))
    return 1 if failures else 0

if __name__ == "__main__": raise SystemExit(main())
