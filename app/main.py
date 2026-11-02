"""FastAPI entrypoint for Research with Receipts orchestrator."""

from __future__ import annotations

import os
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel, Field

from app.orchestrator import run_research

load_dotenv()

app = FastAPI(
    title="Research with Receipts",
    description="Verifiable research briefs — no receipt, no answer. Blast Radius Zero / Pattern B.",
    version="0.1.0",
)


class ResearchRequest(BaseModel):
    question: str = Field(..., min_length=1)


class Claim(BaseModel):
    claim: str
    url: Optional[str] = None
    quote: Optional[str] = None
    screenshot_id: Optional[str] = None
    verdict: Optional[str] = None


class ResearchResponse(BaseModel):
    status: str
    claims: list[dict[str, Any]] = Field(default_factory=list)
    refusal: Optional[str] = None
    sandbox_id: Optional[str] = None
    destroyed: bool = False


@app.get("/")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "research-with-receipts"}


@app.post("/api/research", response_model=ResearchResponse)
def research(body: ResearchRequest) -> ResearchResponse:
    result = run_research(body.question)
    return ResearchResponse(**result)


def create_app() -> FastAPI:
    return app


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=False)
