#!/usr/bin/env bash
# Capture complementary Chrome DevTools MCP evidence for one web reference route.
# Playwright remains the deterministic capture driver; this CLI supplies browser-native
# accessibility snapshots, network requests and performance traces.
set -euo pipefail

BASE_URL="${1:-http://127.0.0.1:18086}"
ROUTE="${2:-/khitmee_sales}"
OUT_DIR="${3:-desktop-frontend/evidence/web-reference/devtools}"
ROUTE_ID="${ROUTE#/}"
ROUTE_ID="${ROUTE_ID//\//-}"
ROUTE_ID="${ROUTE_ID//#/--}"
ROUTE_ID="${ROUTE_ID:-root}"
TARGET_DIR="$OUT_DIR/$ROUTE_ID"
mkdir -p "$TARGET_DIR"

chrome-devtools new_page "$BASE_URL$ROUTE" --timeout 15000 --output-format=json > "$TARGET_DIR/page.json"
PAGE_ID="$(python - <<'PY' "$TARGET_DIR/page.json"
import json,sys
text=open(sys.argv[1],encoding='utf-8').read()
try:
 data=json.loads(text)
 print(data.get('pageId') or data.get('id') or 1)
except Exception:
 print(1)
PY
)"
chrome-devtools resize_page "$PAGE_ID" 1180 760 --output-format=json > "$TARGET_DIR/resize.json" || true
chrome-devtools take_snapshot "$PAGE_ID" --verbose true --filePath "$TARGET_DIR/accessibility-snapshot.txt" --output-format=json > "$TARGET_DIR/snapshot-result.json"
chrome-devtools take_screenshot "$PAGE_ID" --filePath "$TARGET_DIR/devtools.png" --output-format=json > "$TARGET_DIR/screenshot-result.json"
chrome-devtools list_network_requests "$PAGE_ID" --pageSize 100 --output-format=json > "$TARGET_DIR/network-requests.json" || true
chrome-devtools performance_start_trace "$PAGE_ID" --reload true --autoStop true --filePath "$TARGET_DIR/performance-trace.json.gz" --output-format=json > "$TARGET_DIR/performance-result.json" || true

echo "captured Chrome DevTools evidence for $ROUTE into $TARGET_DIR"
