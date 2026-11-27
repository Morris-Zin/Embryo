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
    payload=json.dumps({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"capture-orchestrator","version":"0"}}}).encode()
    req=urllib.request.Request(f"http://127.0.0.1:{port}/mcp",data=payload,headers={"content-type":"application/json"})
    try:
        with urllib.request.urlopen(req,timeout=.3) as response: return 200 <= response.status < 300
    except Exception: return False

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
        process=subprocess.Popen([str(EXE)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            deadline=time.time()+10
            while time.time()<deadline and not ready(port): time.sleep(.05)
            if not ready(port):
                failures.append((screen,state,"native MCP did not start")); continue
            cmd=["node",str(ROOT/"tools"/"capture_native_mcp.mjs"),"--port",str(port),"--screen",screen,"--state",state,"--actions",json.dumps(config["actions"],separators=(",",":"))]
            result=subprocess.run(cmd,cwd=REPO,capture_output=True,text=True,timeout=30)
            if result.returncode:
                failures.append((screen,state,(result.stderr+result.stdout).strip()[-500:]))
        finally:
            process.kill()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: pass
    print(json.dumps({"passed":not failures,"failures":failures},indent=2))
    return 1 if failures else 0

if __name__ == "__main__": raise SystemExit(main())
