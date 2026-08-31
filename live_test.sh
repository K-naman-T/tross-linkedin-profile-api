#!/usr/bin/env bash
# Real end-to-end live test. Requires cookies.json (dedicated, full linkedin.com jar).
# Restarts the server, fetches a REAL public profile over the public HTTPS URL,
# and asserts the response contains a real full_name. No mocking.
# Usage: ./live_test.sh [slug]
set -euo pipefail
cd "$(dirname "$0")"

SLUG=${1:-satyanadella}
URL=$(cat /tmp/public_url.txt 2>/dev/null || echo "https://nyc-direct-jail-those.trycloudflare.com")
API_KEY=${API_KEY:-tross-prod-secret}

if [ ! -f cookies.json ]; then
  echo "ERROR: cookies.json missing. Drop the dedicated full linkedin.com jar here, then rerun." >&2
  exit 1
fi

# Restart uvicorn so it picks up cookies.json (read at startup).
[ -f /tmp/uvicorn.pid ] && kill "$(cat /tmp/uvicorn.pid)" 2>/dev/null || true
sleep 1
setsid bash -c "API_KEY=$API_KEY exec uvicorn main:app --host 127.0.0.1 --port 8099" > /tmp/uvicorn.log 2>&1 < /dev/null &
echo "$!" > /tmp/uvicorn.pid
sleep 4

echo "=== live fetch: $SLUG ==="
RESP=$(curl -s --max-time 30 -H "Authorization: Bearer $API_KEY" "$URL/profile?slug=$SLUG")
echo "$RESP" | head -c 600; echo

NAME=$(echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('data',{}).get('basic',{}).get('full_name',''))" 2>/dev/null || true)
if [ -n "$NAME" ]; then
  echo "LIVE TEST PASS: got real profile for '$NAME'"
else
  echo "LIVE TEST FAIL: no full_name in response (check cookies / rate limit)"
  exit 1
fi
