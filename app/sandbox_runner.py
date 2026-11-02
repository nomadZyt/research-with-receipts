"""
Spawn a disposable Playwright sandbox via `docker run --rm`.

NEVER pass VULTR_INFERENCE_API_KEY (or other orchestrator secrets) into
the container environment. Job I/O is bind-mounted under /job only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

SANDBOX_IMAGE = os.getenv("SANDBOX_IMAGE", "research-with-receipts-sandbox:latest")
SANDBOX_TIMEOUT_SEC = int(os.getenv("SANDBOX_TIMEOUT_SEC", "120"))
SANDBOX_MEMORY_MB = int(os.getenv("SANDBOX_MEMORY_MB", "1024"))
SANDBOX_CPUS = os.getenv("SANDBOX_CPUS", "1")
# Prefer network isolation when possible; use bridge only if outbound fetch is required.
SANDBOX_NETWORK = os.getenv("SANDBOX_NETWORK", "bridge")


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def dispatch_sandbox(urls: list[str]) -> dict[str, Any]:
    sandbox_id = f"rwr-{uuid.uuid4().hex[:12]}"

    if not _docker_available():
        return {
            "sandbox_id": sandbox_id,
            "destroyed": True,
            "skipped": True,
            "results": [],
            "refusal": (
                "Docker not available on this host; sandbox skipped. "
                "Refusing to invent claims without screenshots."
            ),
        }

    host_job = Path(tempfile.mkdtemp(prefix=f"{sandbox_id}-"))
    out_dir = host_job / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    input_path = host_job / "input.json"
    input_path.write_text(json.dumps({"urls": urls}), encoding="utf-8")

    # Explicit empty-ish env: do NOT forward host environ / inference keys.
    container_env: list[str] = [
        "-e",
        "HOME=/tmp",
        "-e",
        "PLAYWRIGHT_BROWSERS_PATH=/ms-playwright",
    ]

    cmd = [
        "docker",
        "run",
        "--rm",
        "--name",
        sandbox_id,
        "--network",
        SANDBOX_NETWORK,
        "--memory",
        f"{SANDBOX_MEMORY_MB}m",
        "--cpus",
        SANDBOX_CPUS,
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=256m",
        "-v",
        f"{host_job}:/job",
        *container_env,
        SANDBOX_IMAGE,
    ]

    destroyed = False
    results: list[dict[str, Any]] = []
    raw_stdout = ""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SANDBOX_TIMEOUT_SEC,
            check=False,
            # Host env is for the docker CLI process only; secrets are not
            # injected via -e above. Do not add VULTR_INFERENCE_API_KEY here.
        )
        raw_stdout = (proc.stdout or "").strip()
        if raw_stdout:
            try:
                summary = json.loads(raw_stdout.splitlines()[-1])
                results = summary.get("results") or []
            except json.JSONDecodeError:
                results = []
        # Enrich from files if stdout sparse
        if not results:
            for i, url in enumerate(urls):
                meta_path = out_dir / f"{i}.json"
                png_path = out_dir / f"{i}.png"
                if meta_path.is_file():
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    results.append(
                        {
                            "index": i,
                            "url": meta.get("url", url),
                            "ok": "error" not in meta and png_path.is_file(),
                            "title": meta.get("title"),
                            "quote": (meta.get("text") or "")[:500],
                            "screenshot": str(png_path) if png_path.is_file() else None,
                        }
                    )
    except subprocess.TimeoutExpired:
        subprocess.run(
            ["docker", "rm", "-f", sandbox_id],
            capture_output=True,
            check=False,
        )
        results = []
    finally:
        destroyed = True
        # Best-effort cleanup of host job dir is left to caller / tmp cleaner.
        # Screenshots can be copied to ./screenshots by a later step.

    return {
        "sandbox_id": sandbox_id,
        "destroyed": destroyed,
        "skipped": False,
        "results": results,
        "raw_stdout": raw_stdout[:2000] if raw_stdout else "",
        "job_dir": str(host_job),
    }
