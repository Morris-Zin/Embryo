#!/usr/bin/env python3
import json
import sys

data = json.load(sys.stdin)
for container in data:
    config = container.get("Config") or {}
    env = config.get("Env") or []
    config["Env"] = [entry.split("=", 1)[0] + "=<redacted>" for entry in env]
    container["Config"] = config
print(json.dumps(data, indent=2, sort_keys=True))
