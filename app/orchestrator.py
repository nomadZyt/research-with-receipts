"""
Orchestrator stub: plan → dispatch_sandbox(urls) → assemble claims.

Inference key stays here (VM process). Never forward VULTR_INFERENCE_API_KEY
into the sandbox container environment.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote_plus

from app.sandbox_runner import dispatch_sandbox


def plan(question: str) -> list[str]:
    """
    Stub planner. Later: call Vultr Serverless Inference
    (VULTR_INFERENCE_BASE_URL + VULTR_INFERENCE_API_KEY) to propose source URLs.
    """
    # Placeholder URLs for scaffold / local demo — replace with Inference-planned list.
    q = quote_plus(question[:200])
    return [
        f"https://example.com/?q={q}",
        "https://example.org/",
    ]


def assemble_claims(sandbox_result: dict[str, Any]) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for item in sandbox_result.get("results") or []:
        if not item.get("ok"):
            continue
        screenshot = item.get("screenshot")
        if not screenshot:
            continue
        text = item.get("title") or ""
        # Full text lives in out/{i}.json; runner may attach excerpt later.
        quote = (item.get("quote") or item.get("title") or "")[:500]
        if not quote and not screenshot:
            continue
        claims.append(
            {
                "claim": f"Source page titled: {item.get('title') or '(untitled)'}",
                "url": item.get("url"),
                "quote": quote or None,
                "screenshot_id": screenshot,
                "verdict": "pending_vision_check",
            }
        )
    return claims


def run_research(question: str) -> dict[str, Any]:
    urls = plan(question)
    sandbox_result = dispatch_sandbox(urls)
    sandbox_id = sandbox_result.get("sandbox_id")
    destroyed = bool(sandbox_result.get("destroyed", False))

    if sandbox_result.get("skipped"):
        # Local stub path when Docker is unavailable — still refuse without real receipts.
        return {
            "status": "refused",
            "claims": [],
            "refusal": sandbox_result.get(
                "refusal",
                "Sandbox did not produce screenshots; refusing to answer without receipts.",
            ),
            "sandbox_id": sandbox_id,
            "destroyed": destroyed,
        }

    claims = assemble_claims(sandbox_result)
    has_screenshots = any(c.get("screenshot_id") for c in claims)

    if not has_screenshots or not claims:
        return {
            "status": "refused",
            "claims": [],
            "refusal": "No screenshots/receipts from sandbox; refusing to answer without receipts.",
            "sandbox_id": sandbox_id,
            "destroyed": destroyed,
        }

    return {
        "status": "ok",
        "claims": claims,
        "refusal": None,
        "sandbox_id": sandbox_id,
        "destroyed": destroyed,
    }


def inference_configured() -> bool:
    return bool(os.getenv("VULTR_INFERENCE_API_KEY"))
