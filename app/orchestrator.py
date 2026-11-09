"""
Plan → disposable Playwright sandbox → verified claims.

Inference stays on this process. VULTR_INFERENCE_API_KEY is never forwarded
into the sandbox. A claim is returned only when its quote is verified
against the captured page (text, or vision when Inference is configured)
and the PNG receipt is on disk. If none survive, the response is a refusal.
Claims and source URLs are never invented for an unsourceable question.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from pydantic import ValidationError

from app.planner import plan_research, stub_can_source
from app.sandbox_runner import PNG_MAGIC, dispatch_sandbox, resolve_screenshot
from app.schemas import (
    Claim,
    PlannerKind,
    PlannedVisit,
    ResearchPlan,
    ResearchResponse,
    SandboxRunResponse,
    ScreenshotReceipt,
    Verdict,
    VerifierKind,
)
from app.verifier import (
    decide_verification,
    longest_term_in_capture,
    numbers_grounded,
    quote_in_capture,
    verification_backend,
    vision_quote_visible,
)

log = logging.getLogger("research_with_receipts.orchestrator")

UNSOURCEABLE_REFUSAL = (
    "No supported source is available for this question. "
    "Refusing to invent claims or citations."
)
UNVERIFIED_REFUSAL = (
    "No claim's quote could be verified against its screenshot receipt. "
    "Refusing to answer."
)
NO_RECEIPT_REFUSAL = (
    "No screenshot receipts with page text; refusing to answer without receipts."
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
    if not path.is_file() or path.stat().st_size <= 8:
        return False
    try:
        with path.open("rb") as handle:
            return handle.read(8) == PNG_MAGIC
    except OSError:
        return False


def _quote_for(receipt: ScreenshotReceipt) -> str:
    return (receipt.quote or "").strip() or (receipt.title or "").strip()


def _statement_for(
    plan: ResearchPlan,
    receipt: ScreenshotReceipt,
    expected: dict[str, str],
) -> str | None:
    statement = expected.get(_url_key(receipt.url))
    if statement:
        return statement
    if 0 <= receipt.index < len(plan.visits):
        return plan.visits[receipt.index].expected_claim
    return None


def _verify_quote(receipt: ScreenshotReceipt, quote: str) -> str | None:
    text_ok = quote_in_capture(quote, receipt)
    vision: bool | None = None
    if verification_backend() == "vision":
        try:
            path = resolve_screenshot(receipt.relative_path)
        except ValueError:
            path = None
        if path is not None:
            vision = vision_quote_visible(path, quote)
    return decide_verification(text_ok, vision)


def assemble_claims(
    plan: ResearchPlan,
    receipts: list[ScreenshotReceipt],
) -> list[Claim]:
    """Return claims whose quote was verified and whose PNG is on disk.

    Stub questions outside the source table produce nothing. Inference plans
    drop a receipt when the capture never mentions the question. Statements
    that introduce digits absent from the capture are dropped. No title-only
    fallback claim is created.
    """
    if plan.planner == PlannerKind.stub and not stub_can_source(plan.question):
        return []
    expected = _expected_claims(plan)
    created_at = datetime.now(timezone.utc)
    claims: list[Claim] = []
    for receipt in receipts:
        if not _receipt_file_ok(receipt):
            continue
        if plan.planner != PlannerKind.stub and not longest_term_in_capture(
            plan.question, receipt
        ):
            continue
        quote = _quote_for(receipt)
        if not quote:
            continue
        kind = _verify_quote(receipt, quote)
        if kind is None:
            continue
        statement = _statement_for(plan, receipt, expected)
        if not statement or not numbers_grounded(statement, receipt):
            continue
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
                    verdict=Verdict.supported,
                    verifier=VerifierKind(kind),
                )
            )
        except ValidationError:
            continue
    return claims


def _verification_label(claims: list[Claim], receipts: list[ScreenshotReceipt]) -> str | None:
    kinds = {claim.verifier.value for claim in claims if claim.verifier}
    if kinds == {"vision"}:
        return "vision"
    if "vision" in kinds and "text" in kinds:
        return "mixed"
    if kinds:
        return "text"
    if any(item.ok for item in receipts):
        return verification_backend()
    return None


def _refused(
    plan: ResearchPlan,
    *,
    refusal: str,
    refusal_code: str,
    sandbox_result: dict[str, Any] | None = None,
    receipts: list[ScreenshotReceipt] | None = None,
    visits: list[PlannedVisit] | None = None,
    note: str | None = None,
    verification: str | None = None,
) -> ResearchResponse:
    sandbox_id = None
    destroyed = False
    if sandbox_result is not None:
        sandbox_id = sandbox_result.get("sandbox_id")
        destroyed = bool(sandbox_result.get("destroyed", False))
    return ResearchResponse(
        status="refused",
        question=plan.question,
        planner=plan.planner,
        planned_at=plan.planned_at,
        visits=list(plan.visits) if visits is None else visits,
        claims=[],
        receipts=list(receipts or []),
        refusal=refusal,
        refusal_code=refusal_code,
        note=plan.note if note is None else note,
        sandbox_id=sandbox_id,
        destroyed=destroyed,
        verification=verification,
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
    if plan.planner == PlannerKind.stub and not stub_can_source(plan.question):
        note = "Unsourceable question. No pages were opened and no claims were created."
        if plan.note and "fell back" in plan.note:
            note = f"{plan.note} {note}"
        log.info("research refused code=unsourceable")
        return _refused(
            plan,
            refusal=UNSOURCEABLE_REFUSAL,
            refusal_code="unsourceable",
            visits=[],
            note=note,
        )

    urls = [visit.url for visit in plan.visits]
    sandbox_result = dispatch_sandbox(urls)
    receipts = _coerce_receipts(list(sandbox_result.get("receipts") or []))
    claims = assemble_claims(plan, receipts)
    verification = _verification_label(claims, receipts)
    sandbox_id = sandbox_result.get("sandbox_id")
    destroyed = bool(sandbox_result.get("destroyed", False))

    if claims:
        log.info(
            "research ok claims=%s verification=%s sandbox=%s",
            len(claims),
            verification,
            sandbox_id,
        )
        return ResearchResponse(
            status="ok",
            question=plan.question,
            planner=plan.planner,
            planned_at=plan.planned_at,
            visits=list(plan.visits),
            claims=claims,
            receipts=receipts,
            refusal=None,
            note=plan.note,
            sandbox_id=sandbox_id,
            destroyed=destroyed,
            verification=verification,
        )

    if any(item.ok for item in receipts):
        refusal = UNVERIFIED_REFUSAL
        code = "unverified"
    else:
        refusal = sandbox_result.get("refusal") or NO_RECEIPT_REFUSAL
        code = "no_receipt"
    log.info("research refused code=%s sandbox=%s", code, sandbox_id)
    return _refused(
        plan,
        refusal=refusal,
        refusal_code=code,
        sandbox_result=sandbox_result,
        receipts=receipts,
        verification=verification,
    )
