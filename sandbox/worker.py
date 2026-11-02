#!/usr/bin/env python3
"""
Sandbox browser worker — runs INSIDE the disposable Docker container.

Reads job JSON from stdin or /job/input.json:
  { "urls": ["https://example.com", ...] }

For each URL: Chromium visit → full-page screenshot → title + first 2k chars text.
Writes /job/out/{i}.png and /job/out/{i}.json; prints summary JSON to stdout.

Hard timeout: enforced by the host orchestrator (docker run --stop-timeout /
SANDBOX_TIMEOUT_SEC kill). This process should not assume unlimited runtime.

Secret hygiene: do not read host env secrets (especially VULTR_INFERENCE_API_KEY).
Only browser-related env (if any) should be present in this container.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

OUT_DIR = Path("/job/out")
INPUT_PATH = Path("/job/input.json")
TEXT_LIMIT = 2000


def load_job() -> dict[str, Any]:
    if not sys.stdin.isatty():
        raw = sys.stdin.read()
        if raw.strip():
            return json.loads(raw)
    if INPUT_PATH.is_file():
        return json.loads(INPUT_PATH.read_text(encoding="utf-8"))
    return {"urls": []}


def write_error(message: str, detail: str | None = None) -> None:
    payload = {"status": "error", "error": message}
    if detail:
        payload["detail"] = detail
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "error.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))


def extract_text(page) -> str:
    try:
        text = page.inner_text("body")
    except Exception:
        text = page.content()
    text = " ".join((text or "").split())
    return text[:TEXT_LIMIT]


def main() -> int:
    # Do not pull inference / cloud API keys from the environment.
    # Host must never pass VULTR_INFERENCE_API_KEY into this container.
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        write_error(
            "playwright_import_failed",
            f"{exc}. Install playwright in the sandbox image "
            "(base mcr.microsoft.com/playwright/python should include it).",
        )
        return 1

    try:
        job = load_job()
    except json.JSONDecodeError as exc:
        write_error("invalid_job_json", str(exc))
        return 1

    urls = job.get("urls") or []
    if not isinstance(urls, list):
        write_error("invalid_urls", "urls must be a list")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for i, url in enumerate(urls):
                item: dict[str, Any] = {
                    "index": i,
                    "url": url,
                    "ok": False,
                    "screenshot": None,
                    "json": None,
                }
                try:
                    page = browser.new_page()
                    page.goto(str(url), wait_until="domcontentloaded", timeout=60_000)
                    title = page.title()
                    text = extract_text(page)
                    png_path = OUT_DIR / f"{i}.png"
                    json_path = OUT_DIR / f"{i}.json"
                    page.screenshot(path=str(png_path), full_page=True)
                    meta = {
                        "index": i,
                        "url": url,
                        "title": title,
                        "text": text,
                        "screenshot": str(png_path),
                    }
                    json_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
                    item.update(
                        {
                            "ok": True,
                            "title": title,
                            "text_len": len(text),
                            "screenshot": str(png_path),
                            "json": str(json_path),
                        }
                    )
                    page.close()
                except Exception as exc:  # per-URL failure should not kill the whole job
                    item["error"] = str(exc)
                    err_path = OUT_DIR / f"{i}.json"
                    err_path.write_text(
                        json.dumps(
                            {"index": i, "url": url, "error": str(exc)},
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                    item["json"] = str(err_path)
                results.append(item)
        finally:
            browser.close()

    summary = {
        "status": "ok",
        "count": len(results),
        "results": results,
        # Host enforces hard timeout via SANDBOX_TIMEOUT_SEC / docker stop.
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
