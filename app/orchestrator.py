"""
Plan → disposable Playwright sandbox → claims with screenshot receipts.

Inference stays on this process. VULTR_INFERENCE_API_KEY is never forwarded
into the sandbox. With an empty key, planning is the deterministic stub.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from pydantic import ValidationError

from app.planner import plan_research
from app.sandbox_runner import dispatch_sandbox, resolve_screenshot
from app.schemas import (
    Claim,
    ResearchPlan,
    ResearchResponse,
    SandboxRunResponse,
    ScreenshotReceipt,
    Verdict,
)


def _url_key(url: str) -> str:
    parsed = urlparse(url.strip())
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}{query}"


def _expected_claims(plan: ResearchPlan) -> dict[str, str]:
    found: dict[str, str] = {}
    for visit in plan.visits:
        found[_url_key(visit.url)] = visit.expected_claim
    return found


def _coerce_receipts(raw_items: list[dict[str, Any]]) -> list[ScreenshotReceipt]:
    receipts: list[ScreenshotReceipt] = []
    for raw in raw_items:
        try:
            receipts.append(ScreenshotReceipt.model_validate(raw))
        except ValidationError:
            continue
    return receipts


def _receipt_file_ok(receipt: ScreenshotReceipt) -> bool:
    if not receipt.ok or receipt.byte_size <= 0:
        return False
    try:
        path = resolve_screenshot(receipt.relative_path)
    except ValueError:
        return False
    return path.is_file() and path.stat().st_size > 0


def assemble_claims(
    plan: ResearchPlan,
    receipts: list[ScreenshotReceipt],
) -> list[Claim]:
    """Build claims only for receipts that have a quote and a PNG on disk."""
    expected = _expected_claims(plan)
    created_at = datetime.now(timezone.utc)
    claims: list[Claim] = []
    for receipt in receipts:
        if not _receipt_file_ok(receipt):
            continue
        quote = (receipt.quote or "").strip() or (receipt.title or "").strip()
        if not quote:
            continue
        statement = expected.get(_url_key(receipt.url))
        if not statement:
            title = receipt.title or "Untitled page"
            statement = f"Captured page “{title}” at {receipt.url}."
        try:
            claims.append(
                Claim(
                    claim_id=f"clm-{uuid.uuid4().hex[:12]}",
                    claim=statement[:1000],
                    url=receipt.url,
                    quote=quote[:2000],
                    screenshot_id=receipt.screenshot_id,
                    screenshot_path=receipt.relative_path,
                    captured_at=receipt.captured_at,
                    created_at=created_at,
                    source_title=receipt.title,
                    verdict=Verdict.pending_vision_check,
                )
            )
        except ValidationError:
            continue
    return claims


def _refusal_for(sandbox_result: dict[str, Any], claims: list[Claim]) -> str | None:
    if claims:
        return None
    return sandbox_result.get("refusal") or (
        "No screenshot receipts with page text; refusing to answer without receipts."
    )


def run_sandbox(urls: list[str]) -> SandboxRunResponse:
    """URL → sandbox → screenshot receipts. Does not plan or call Inference."""
    result = dispatch_sandbox(urls)
    receipts = _coerce_receipts(list(result.get("receipts") or []))
    refusal = result.get("refusal")
    if any(item.ok for item in receipts):
        refusal = None
    elif not refusal:
        refusal = "Sandbox produced no screenshot receipts."
    return SandboxRunResponse(
        sandbox_id=str(result.get("sandbox_id")),
        destroyed=bool(result.get("destroyed", False)),
        skipped=bool(result.get("skipped", False)),
        receipts=receipts,
        refusal=refusal,
    )


def run_research(question: str) -> ResearchResponse:
    plan = plan_research(question)
    urls = [visit.url for visit in plan.visits]
    sandbox_result = dispatch_sandbox(urls)
    receipts = _coerce_receipts(list(sandbox_result.get("receipts") or []))
    claims = assemble_claims(plan, receipts)
    status = "ok" if claims else "refused"
    return ResearchResponse(
        status=status,
        question=plan.question,
        planner=plan.planner,
        planned_at=plan.planned_at,
        visits=list(plan.visits),
        claims=claims,
        receipts=receipts,
        refusal=_refusal_for(sandbox_result, claims),
        note=plan.note,
        sandbox_id=sandbox_result.get("sandbox_id"),
        destroyed=bool(sandbox_result.get("destroyed", False)),
    )

