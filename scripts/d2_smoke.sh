#!/usr/bin/env bash
# Hit the D2 local path. Inference key is not required.
# Start the API first: docker compose up --build
#   or: uvicorn app.main:app --host 0.0.0.0 --port 8080
# Usage: scripts/d2_smoke.sh [base_url]

set -euo pipefail

BASE="${1:-${BASE_URL:-http://127.0.0.1:8080}}"

echo "=== GET ${BASE}/ ==="
curl -fsS "${BASE}/"
echo
echo

echo "=== POST /api/plan (stub planner, no Inference key) ==="
curl -fsS "${BASE}/api/plan" \
  -H 'content-type: application/json' \
  -d '{"question":"What does example.com say it is for?"}'
echo
echo

echo "=== POST /api/sandbox/run (Playwright receipts) ==="
SANDBOX="$(curl -fsS "${BASE}/api/sandbox/run" \
  -H 'content-type: application/json' \
  -d '{"urls":["https://example.com/"]}')"
echo "${SANDBOX}"
echo

python3 - <<'PY' "${SANDBOX}"
import json, sys
body = json.loads(sys.argv[1])
ok = [item for item in body.get("receipts") or [] if item.get("ok")]
if not ok:
    raise SystemExit("sandbox run produced no screenshot receipt: " + json.dumps(body)[:800])
print("receipt", ok[0]["relative_path"], "bytes", ok[0]["byte_size"])
PY

echo
echo "=== POST /api/claims/validate ==="
curl -fsS "${BASE}/api/claims/validate" \
  -H 'content-type: application/json' \
  -d '{"claim_id":"clm-smoke000001","claim":"example.com is reserved for examples.","url":"https://example.com/","quote":"Example Domain","screenshot_id":"rwr-0123456789ab-0","screenshot_path":"screenshots/rwr-0123456789ab/0.png","captured_at":"2026-11-04T03:00:00Z","created_at":"2026-11-04T03:00:01Z","verdict":"pending_vision_check"}'
echo
echo
echo "Screenshots are files under ./screenshots/<sandbox_id>/*.png"
echo "Fetch one with GET /api/screenshots/<sandbox_id>/<index>.png"
