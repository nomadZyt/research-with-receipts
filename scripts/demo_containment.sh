#!/usr/bin/env bash
# Demo narrative: containment moment for Blast Radius Zero / Pattern B.
# Comments + inspect-style checks only — does not invent credentials.
# Run from repo root after: docker compose build sandbox

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

IMAGE="${SANDBOX_IMAGE:-research-with-receipts-sandbox:latest}"
NAME="rwr-containment-demo-$$"

echo "=== Research with Receipts — containment demo ==="
echo "0) Sandbox env whitelist (no Docker, no live Inference call)."
if ! python3 -c "import pydantic" >/dev/null 2>&1; then
  echo "Install dependencies first: pip install -r requirements.txt"
  exit 1
fi
export VULTR_INFERENCE_API_KEY="containment-demo-secret"
PYTHONPATH="${ROOT}" python3 - <<'PY'
import os
from pathlib import Path

from app.sandbox_runner import (
    build_docker_command,
    container_env_flags,
    docker_cli_env,
)

secret = os.environ["VULTR_INFERENCE_API_KEY"]
flags = container_env_flags()
blob = " ".join(flags)
cli = docker_cli_env()
cmd = build_docker_command(
    sandbox_id="rwr-demo0000001",
    host_job=Path("/tmp/job"),
    settings={
        "image": "research-with-receipts-sandbox:latest",
        "memory_mb": 128,
        "cpus": "1",
        "network": "none",
        "shm": "64m",
    },
)
command = " ".join(cmd)
assert "VULTR_INFERENCE_API_KEY" not in blob, blob
assert secret not in blob
assert "VULTR_INFERENCE_API_KEY" not in cli
assert secret not in " ".join(cli.values())
assert secret not in command
assert "VULTR_INFERENCE_API_KEY" not in command
assert "--env-file" not in cmd
assert "HOME=/tmp" in flags
print("whitelist ok:", blob)
PY
unset VULTR_INFERENCE_API_KEY
echo
echo "1) Inference API key must NEVER appear in sandbox env."
echo "   Orchestrator keeps VULTR_INFERENCE_API_KEY on the VM only."
echo "   sandbox_runner.py does not pass that variable via docker -e."
echo

echo "2) Attempt narrative: container should not write arbitrary host paths."
echo "   We mount ONLY a temp /job directory; rootfs may be --read-only."
echo

JOB_DIR="$(mktemp -d)"
mkdir -p "${JOB_DIR}/out"
echo '{"urls":["https://example.com/"]}' > "${JOB_DIR}/input.json"

echo "3) Launch sandbox without secret env (note: no VULTR_INFERENCE_API_KEY in -e)."
# Intentionally omit any inference/cloud API keys from -e flags.
docker run -d --name "${NAME}" \
  --network none \
  --memory "${SANDBOX_MEMORY_MB:-1024}m" \
  --cpus "${SANDBOX_CPUS:-1}" \
  --read-only \
  --tmpfs /tmp:rw,size=64m \
  -v "${JOB_DIR}:/job" \
  -e HOME=/tmp \
  "${IMAGE}" \
  sleep 30 >/dev/null 2>&1 || {
    echo "(Image may be missing — build with: docker compose build sandbox)"
    echo "Narrative still holds: runner policy forbids secret env injection."
    rm -rf "${JOB_DIR}"
    exit 0
  }

echo "4) docker inspect — Config.Env should lack VULTR_INFERENCE_API_KEY:"
docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "${NAME}" | grep -i VULTR || echo "(no VULTR_* in container env — good)"

echo
echo "5) Network mode / mounts (expect network=none here; job volume only):"
docker inspect -f 'NetworkMode={{.HostConfig.NetworkMode}} Memory={{.HostConfig.Memory}} ReadonlyRootfs={{.HostConfig.ReadonlyRootfs}}' "${NAME}"
docker inspect -f '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}' "${NAME}"

echo
echo "6) Destroy sandbox and show receipt."
docker rm -f "${NAME}" >/dev/null
echo "destroyed=true sandbox_was=${NAME}"
rm -rf "${JOB_DIR}"

echo
echo "7) Hostile narrative (do not run for real in demos without care):"
echo "   # docker run ... -v /:/host ...   ← policy: NEVER bind host root"
echo "   # docker run ... -e VULTR_INFERENCE_API_KEY=...  ← policy: NEVER"
echo "   Demo UI should show sandbox_id + destroyed=true after each research job."
echo "=== end containment demo ==="
