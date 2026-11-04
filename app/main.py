"""FastAPI entrypoint for Research with Receipts orchestrator."""

from __future__ import annotations

import os
import re

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from app.inference import inference_configured
from app.orchestrator import run_research, run_sandbox
from app.planner import plan_research
from app.sandbox_runner import screenshots_dir
from app.schemas import (
    Claim,
    ResearchPlan,
    ResearchRequest,
    ResearchResponse,
    SandboxRunRequest,
    SandboxRunResponse,
)

load_dotenv()

app = FastAPI(
    title="Research with Receipts",
    description=(
        "Verifiable research briefs — no receipt, no answer. "
        "Blast Radius Zero / Pattern B. "
        "D2 runs planning, a disposable Playwright sandbox, and claim validation. "
        "Vultr Serverless Inference stays unwired unless VULTR_INFERENCE_API_KEY is set."
    ),
    version="0.2.0",
)

_SANDBOX_ID = re.compile(r"rwr-[a-f0-9]{12}")
_PNG_NAME = re.compile(r"[0-9]+\.png")


@app.get("/")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "research-with-receipts",
        "inference": "configured" if inference_configured() else "stub",
    }


@app.post("/api/plan", response_model=ResearchPlan)
def plan(body: ResearchRequest) -> ResearchPlan:
    """Plan URLs for a question. Uses the stub planner when no Inference key is set."""
    return plan_research(body.question)


@app.post("/api/research", response_model=ResearchResponse)
def research(body: ResearchRequest) -> ResearchResponse:
    """Plan, screenshot each URL in the sandbox, and return claims that have receipts."""
    return run_research(body.question)


@app.post("/api/sandbox/run", response_model=SandboxRunResponse)
def sandbox_run(body: SandboxRunRequest) -> SandboxRunResponse:
    """Open the given URLs in the Playwright sandbox and return screenshot receipts."""
    return run_sandbox(body.urls)


@app.post("/api/claims/validate", response_model=Claim)
def validate_claim(body: Claim) -> Claim:
    """Validate a claim. Schema only — does not check that the PNG is on disk."""
    return body


@app.get("/api/screenshots/{sandbox_id}/{filename}")
def get_screenshot(sandbox_id: str, filename: str) -> FileResponse:
    """Serve a PNG published under screenshots/<sandbox_id>/<index>.png."""
    if not _SANDBOX_ID.fullmatch(sandbox_id) or not _PNG_NAME.fullmatch(filename):
        raise HTTPException(status_code=404, detail="screenshot not found")
    path = screenshots_dir() / sandbox_id / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="screenshot not found")
    return FileResponse(
        path,
        media_type="image/png",
        filename=filename,
        content_disposition_type="inline",
    )


def create_app() -> FastAPI:
    return app


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=False)
