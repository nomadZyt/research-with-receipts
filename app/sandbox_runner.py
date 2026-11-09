"""
Spawn a disposable Playwright sandbox via `docker run --rm`.

NEVER pass VULTR_INFERENCE_API_KEY (or other orchestrator secrets) into
the container environment. ``container_env_flags`` is a fixed whitelist of
literal values; host env is not copied. Job I/O is bind-mounted under /job only.
Screenshots are copied out to screenshots/<sandbox_id>/<index>.png
before the job directory is removed.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.schemas import MAX_URLS

log = logging.getLogger("research_with_receipts.sandbox")

REPO_ROOT = Path(__file__).resolve().parents[1]
SANDBOX_IMAGE = os.getenv("SANDBOX_IMAGE", "research-with-receipts-sandbox:latest")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def screenshots_dir() -> Path:
    raw = os.getenv("SCREENSHOTS_DIR", "").strip()
    path = Path(raw) if raw else REPO_ROOT / "screenshots"
    path.mkdir(parents=True, exist_ok=True)
    return path


def jobs_dir() -> Path:
    raw = os.getenv("SANDBOX_JOBS_DIR", "").strip()
    path = Path(raw) if raw else REPO_ROOT / "var" / "sandbox-jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _docker_available() -> bool:
    return shutil.which("docker") is not None


# Container environment is this list and nothing else. Values are literals,
# never copied from the host process. Secret names are rejected even if a
# later edit adds them to the tuple.
SANDBOX_ENV_WHITELIST: tuple[tuple[str, str], ...] = (
    ("HOME", "/tmp"),
    ("XDG_CACHE_HOME", "/tmp"),
    ("XDG_CONFIG_HOME", "/tmp"),
    ("PLAYWRIGHT_BROWSERS_PATH", "/ms-playwright"),
)
SANDBOX_ENV_DENY = frozenset(
    {
        "VULTR_INFERENCE_API_KEY",
        "VULTR_API_KEY",
        "VULTR_INFERENCE_MODEL",
        "VULTR_INFERENCE_BASE_URL",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_ACCESS_KEY_ID",
    }
)
_SECRET_MARKERS = ("SECRET", "TOKEN", "PASSWORD", "API_KEY", "CREDENTIAL")


def _is_secret_env_name(name: str) -> bool:
    upper = name.upper()
    if upper in SANDBOX_ENV_DENY or upper.startswith("VULTR_"):
        return True
    return any(marker in upper for marker in _SECRET_MARKERS)


def container_env_flags() -> list[str]:
    """``docker run -e`` flags. Host environment is not consulted."""
    flags: list[str] = []
    for key, value in SANDBOX_ENV_WHITELIST:
        if _is_secret_env_name(key) or not value or "\n" in value or "\x00" in value:
            continue
        flags.extend(["-e", f"{key}={value}"])
    return flags


def docker_cli_env() -> dict[str, str]:
    """Environment for the docker *client* process, with secrets removed.

    Docker does not copy this into the container unless ``-e`` / ``--env-file``
    says so. ``build_docker_command`` does neither for secrets. Dropping the
    keys here keeps them out of the client process as well.
    """
    secret_values = [
        os.getenv(name, "").strip()
        for name in ("VULTR_INFERENCE_API_KEY", "VULTR_API_KEY")
    ]
    secret_values = [value for value in secret_values if len(value) >= 8]
    cleaned: dict[str, str] = {}
    for key, value in os.environ.items():
        if _is_secret_env_name(key):
            continue
        if any(secret in value for secret in secret_values):
            continue
        cleaned[key] = value
    return cleaned


def build_docker_command(
    *,
    sandbox_id: str,
    host_job: Path,
    settings: dict[str, Any],
) -> list[str]:
    """Assemble ``docker run`` without ``--env-file`` or host secret ``-e``."""
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        sandbox_id,
        "--network",
        str(settings["network"]),
        "--memory",
        f"{settings['memory_mb']}m",
        "--cpus",
        str(settings["cpus"]),
        "--shm-size",
        str(settings["shm"]),
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,size=512m",
        "-v",
        f"{host_job}:/job",
        *container_env_flags(),
        str(settings["image"]),
    ]


def _settings() -> dict[str, Any]:
    return {
        "image": os.getenv("SANDBOX_IMAGE", SANDBOX_IMAGE),
        "timeout": int(os.getenv("SANDBOX_TIMEOUT_SEC", "120")),
        "memory_mb": int(os.getenv("SANDBOX_MEMORY_MB", "1024")),
        "cpus": os.getenv("SANDBOX_CPUS", "1"),
        "network": os.getenv("SANDBOX_NETWORK", "bridge"),
        "shm": os.getenv("SANDBOX_SHM_SIZE", "256m"),
    }


def _unescape_mount(value: str) -> str:
    return value.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n")


def _mount_entries() -> list[tuple[str, str]]:
    """Return (mount_point, root_on_device) from /proc/self/mountinfo."""
    path = Path("/proc/self/mountinfo")
    if not path.is_file():
        return []
    entries: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        root = _unescape_mount(parts[3])
        mount_point = _unescape_mount(parts[4])
        entries.append((mount_point, root))
    return entries


def translate_to_docker_host_path(path: Path) -> Path:
    """Map a path this process can see to a path the Docker daemon can mount.

    On the host the path is already a daemon path. Inside Compose, the job
    directory is a bind mount and mountinfo records the host source.
    ``HOST_SANDBOX_JOBS_DIR`` overrides detection for that directory.
    """
    resolved = path.resolve()
    explicit = os.getenv("HOST_SANDBOX_JOBS_DIR", "").strip()
    if explicit:
        try:
            relative = resolved.relative_to(jobs_dir().resolve())
        except ValueError:
            relative = None
        if relative is not None:
            return Path(explicit) / relative

    matches: list[tuple[int, str, Path]] = []
    for mount_point, root in _mount_entries():
        mount = mount_point.rstrip("/") or "/"
        try:
            relative = resolved.relative_to(Path(mount))
        except ValueError:
            continue
        matches.append((len(mount), root, relative))
    if not matches:
        return resolved
    _, root, relative = max(matches, key=lambda item: item[0])
    if root == "/":
        return Path("/") / relative
    if str(relative) in {"", "."}:
        return Path(root)
    return Path(root) / relative


def resolve_screenshot(relative_path: str) -> Path:
    """Map a screenshots/... receipt path onto SCREENSHOTS_DIR."""
    normalized = relative_path.replace("\\", "/")
    prefix = "screenshots/"
    if not normalized.startswith(prefix):
        raise ValueError(relative_path)
    relative = Path(normalized[len(prefix) :])
    if any(part == ".." for part in relative.parts):
        raise ValueError(relative_path)
    root = screenshots_dir().resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(relative_path)
    return candidate


def _looks_like_png(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(8) == PNG_MAGIC and path.stat().st_size > 8
    except OSError:
        return False


def _parse_captured_at(meta: dict[str, Any]) -> str:
    raw = meta.get("captured_at")
    if isinstance(raw, str) and raw.strip():
        return raw
    return datetime.now(timezone.utc).isoformat()


def publish_receipts(
    *,
    sandbox_id: str,
    out_dir: Path,
    urls: list[str],
) -> list[dict[str, Any]]:
    """Copy sandbox PNGs into screenshots/<sandbox_id>/ and describe them."""
    dest = screenshots_dir() / sandbox_id
    dest.mkdir(parents=True, exist_ok=True)
    receipts: list[dict[str, Any]] = []
    for index, url in enumerate(urls[:MAX_URLS]):
        meta_path = out_dir / f"{index}.json"
        png_path = out_dir / f"{index}.png"
        meta: dict[str, Any] = {}
        if meta_path.is_file():
            try:
                loaded = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    meta = loaded
            except json.JSONDecodeError:
                meta = {"error": "sandbox wrote invalid json"}
        error = meta.get("error")
        error_text = str(error) if error else None
        byte_size = 0
        published = False
        if png_path.is_file() and _looks_like_png(png_path):
            target = dest / f"{index}.png"
            shutil.copyfile(png_path, target)
            byte_size = target.stat().st_size
            published = byte_size > 8 and not error_text
        raw_text = meta.get("text")
        raw_quote = meta.get("quote")
        if not isinstance(raw_text, str):
            raw_text = "" if raw_text is None else str(raw_text)
        if not isinstance(raw_quote, str):
            raw_quote = "" if raw_quote is None else str(raw_quote)
        if not raw_quote:
            raw_quote = raw_text
        quote = " ".join(raw_quote.split())[:500] or None
        page_text = " ".join((raw_text or raw_quote).split())[:2000] or None
        title = meta.get("title")
        if title is not None:
            title = str(title)[:300] or None
        page_url = meta.get("url") if isinstance(meta.get("url"), str) else url
        receipts.append(
            {
                "screenshot_id": f"{sandbox_id}-{index}",
                "sandbox_id": sandbox_id,
                "index": index,
                "url": page_url,
                "relative_path": f"screenshots/{sandbox_id}/{index}.png",
                "title": title,
                "quote": quote,
                "page_text": page_text,
                "captured_at": _parse_captured_at(meta),
                "byte_size": byte_size,
                "ok": published,
                "error": error_text,
            }
        )
    return receipts


def _job_error(out_dir: Path) -> str | None:
    path = out_dir / "error.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "sandbox wrote invalid error.json"
    if isinstance(payload, dict):
        message = payload.get("error") or "sandbox error"
        detail = payload.get("detail")
        if detail:
            return f"{message}: {detail}"[:500]
        return str(message)[:500]
    return "sandbox error"


def _keep_jobs() -> bool:
    return os.getenv("SANDBOX_KEEP_JOBS", "").strip().lower() in {"1", "true", "yes"}


def dispatch_sandbox(urls: list[str]) -> dict[str, Any]:
    """Run urls inside a throwaway Playwright container and publish receipts."""
    sandbox_id = f"rwr-{uuid.uuid4().hex[:12]}"
    if len(urls) > MAX_URLS:
        urls = urls[:MAX_URLS]

    if not _docker_available():
        log.warning("sandbox %s skipped: docker is unavailable", sandbox_id)
        return {
            "sandbox_id": sandbox_id,
            "destroyed": True,
            "skipped": True,
            "receipts": [],
            "refusal": (
                "Docker is not available on this host; sandbox was not started. "
                "Refusing to invent screenshots."
            ),
        }

    settings = _settings()
    job_dir = jobs_dir() / sandbox_id
    out_dir = job_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "input.json").write_text(
        json.dumps({"urls": urls}, ensure_ascii=False),
        encoding="utf-8",
    )
    host_job = translate_to_docker_host_path(job_dir)

    # Whitelist only. Do not forward the host environment or inference keys.
    cmd = build_docker_command(
        sandbox_id=sandbox_id,
        host_job=host_job,
        settings=settings,
    )

    destroyed = False
    receipts: list[dict[str, Any]] = []
    refusal: str | None = None
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=int(settings["timeout"]),
            check=False,
            env=docker_cli_env(),
        )
        receipts = publish_receipts(sandbox_id=sandbox_id, out_dir=out_dir, urls=urls)
        if not any(item["ok"] for item in receipts):
            detail = _job_error(out_dir) or (proc.stderr or "").strip()[-400:]
            if proc.returncode != 0 and "Unable to find image" in (proc.stderr or ""):
                refusal = (
                    "Sandbox image is missing. Build it with: "
                    "docker compose build sandbox"
                )
            elif detail:
                refusal = f"Sandbox produced no screenshot receipts. {detail}"[:500]
            else:
                refusal = "Sandbox produced no screenshot receipts."
    except subprocess.TimeoutExpired:
        subprocess.run(
            ["docker", "rm", "-f", sandbox_id],
            capture_output=True,
            check=False,
            env=docker_cli_env(),
        )
        receipts = publish_receipts(sandbox_id=sandbox_id, out_dir=out_dir, urls=urls)
        if not any(item["ok"] for item in receipts):
            refusal = (
                f"Sandbox exceeded SANDBOX_TIMEOUT_SEC={settings['timeout']} "
                "before any screenshot was published."
            )
    finally:
        destroyed = True
        if not _keep_jobs():
            shutil.rmtree(job_dir, ignore_errors=True)

    ok_count = sum(1 for item in receipts if item.get("ok"))
    log.info(
        "sandbox %s destroyed=%s receipts=%s ok=%s",
        sandbox_id,
        destroyed,
        len(receipts),
        ok_count,
    )
    return {
        "sandbox_id": sandbox_id,
        "destroyed": destroyed,
        "skipped": False,
        "receipts": receipts,
        "refusal": refusal,
    }
