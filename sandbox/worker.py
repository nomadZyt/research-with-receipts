#!/usr/bin/env python3
"""
Sandbox browser worker — runs INSIDE the disposable Docker container.

Reads /job/input.json (or a JSON document on stdin):
  { "urls": ["https://example.com/", ...] }

For each http(s) URL: Chromium visit, full-page PNG, title, quote, timestamp.
Writes /job/out/{i}.png and /job/out/{i}.json. Prints a short summary to stdout.

The host orchestrator copies PNGs to screenshots/<sandbox_id>/. This process
only writes under /job. It must not read VULTR_INFERENCE_API_KEY; the host
does not pass that variable in.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

OUT_DIR = Path("/job/out")
INPUT_PATH = Path("/job/input.json")
TEXT_LIMIT = 2000
QUOTE_LIMIT = 500
MAX_URLS = 5
PAGE_TIMEOUT_MS = 45_000


def load_job() -> dict[str, Any]:
    if not sys.stdin.isatty():
        raw = sys.stdin.read()
        if raw.strip():
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                return loaded
    if INPUT_PATH.is_file():
        loaded = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            return loaded
    return {"urls": []}


def write_error(message: str, detail: str | None = None) -> None:
    payload: dict[str, Any] = {"status": "error", "error": message}
    if detail:
        payload["detail"] = detail[:500]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "error.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False))


def check_urls(urls: Any) -> list[str]:
    """Accept only absolute http(s) URLs. Reject file:, javascript:, credentials."""
    if not isinstance(urls, list):
        raise ValueError("urls must be a list")
    if len(urls) > MAX_URLS:
        raise ValueError(f"at most {MAX_URLS} urls")
    cleaned: list[str] = []
    for url in urls:
        if not isinstance(url, str):
            raise ValueError("each url must be a string")
        text = url.strip()
        parsed = urlparse(text)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"refusing non-http url: {text}")
        if parsed.username or parsed.password:
            raise ValueError("refusing url with embedded credentials")
        cleaned.append(text)
    return cleaned


def extract_text(page: Any) -> str:
    try:
        text = page.inner_text("body")
    except Exception:
        text = page.content()
    return " ".join((text or "").split())[:TEXT_LIMIT]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def visit(page: Any, index: int, url: str) -> dict[str, Any]:
    captured_at = now_iso()
    item: dict[str, Any] = {
        "index": index,
        "url": url,
        "ok": False,
        "screenshot": None,
        "json": None,
        "captured_at": captured_at,
    }
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        title = page.title() or ""
        text = extract_text(page)
        quote = text[:QUOTE_LIMIT]
        png_path = OUT_DIR / f"{index}.png"
        json_path = OUT_DIR / f"{index}.json"
        page.screenshot(path=str(png_path), full_page=True, timeout=PAGE_TIMEOUT_MS)
        meta = {
            "index": index,
            "url": url,
            "title": title,
            "quote": quote,
            "text": text,
            "captured_at": captured_at,
            "screenshot": str(png_path),
            "bytes": png_path.stat().st_size if png_path.is_file() else 0,
        }
        json_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        item.update(
            {
                "ok": png_path.is_file() and png_path.stat().st_size > 0,
                "title": title,
                "text_len": len(text),
                "screenshot": str(png_path),
                "json": str(json_path),
            }
        )
    except Exception as exc:  # one bad URL must not abort the rest of the job
        item["error"] = str(exc)[:500]
        err_path = OUT_DIR / f"{index}.json"
        err_path.write_text(
            json.dumps(
                {
                    "index": index,
                    "url": url,
                    "error": item["error"],
                    "captured_at": captured_at,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        item["json"] = str(err_path)
    return item


def main() -> int:
    # Do not read inference or cloud API keys. The host must not pass them in.
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        write_error("playwright_import_failed", str(exc))
        return 1

    try:
        job = load_job()
    except json.JSONDecodeError as exc:
        write_error("invalid_job_json", str(exc))
        return 1

    try:
        urls = check_urls(job.get("urls") or [])
    except ValueError as exc:
        write_error("invalid_urls", str(exc))
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            try:
                for index, url in enumerate(urls):
                    page = context.new_page()
                    try:
                        results.append(visit(page, index, url))
                    finally:
                        page.close()
            finally:
                context.close()
                browser.close()
    except Exception as exc:
        write_error("browser_launch_failed", str(exc))
        return 1

    summary = {"status": "ok", "count": len(results), "results": results}
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
