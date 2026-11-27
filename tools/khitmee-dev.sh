#!/usr/bin/env bash
set -eu
cd -- "$(dirname -- "$0")/.."
if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys' >/dev/null 2>&1; then
  PYBIN=(python3)
elif command -v python >/dev/null 2>&1 && python -c 'import sys' >/dev/null 2>&1; then
  PYBIN=(python)
elif command -v py >/dev/null 2>&1 && py -3 -c 'import sys' >/dev/null 2>&1; then
  PYBIN=(py -3)
else
  echo "python3 or python is required" >&2
  exit 2
fi
if [[ "${1:-}" == "parity" ]]; then
  if [[ "${COMPOSE_PROJECT_NAME:-khitmee-perf}" != "khitmee-perf" ]]; then
    echo "Refusing parity: COMPOSE_PROJECT_NAME must be khitmee-perf" >&2
    exit 2
  fi
  shift
  exec "${PYBIN[@]}" -B tools/perf_parity.py "$@"
fi
exec "${PYBIN[@]}" -B tools/khitmee_dev.py "$@"
