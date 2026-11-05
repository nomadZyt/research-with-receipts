"""URL planner.

The planning shape is always a ``ResearchPlan``: the question plus the
URLs the sandbox should open, why each URL was chosen, and the claim the
page is expected to support.

When ``VULTR_INFERENCE_API_KEY`` is empty (the D2 default) a deterministic
stub picks those URLs. The same question always yields the same visits,
and the stub never returns example.com as the only page. When a key and
model id are both set, the Inference client is called and any failure
falls back to the same stub.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone

from pydantic import ValidationError

from app.inference import (
    InferenceError,
    InferenceNotConfigured,
    complete_chat,
    inference_configured,
)
from app.schemas import MAX_URLS, PlannedVisit, PlannerKind, ResearchPlan

_VisitRow = tuple[str, str, str]

# Stable, small public pages so a local sandbox run can collect receipts
# without an Inference key. example.com is in the pool, never the only URL.
_POOL: tuple[_VisitRow, ...] = (
    (
        "https://example.com/",
        "Capture the reserved example.com page as a stable HTML receipt.",
        "example.com is reserved for use in illustrative examples in documents.",
    ),
    (
        "https://example.org/",
        "Capture the reserved example.org page as a second HTML receipt.",
        "example.org is reserved for use in illustrative examples in documents.",
    ),
    (
        "https://httpbin.org/html",
        "Capture the small httpbin HTML fixture and excerpt its text.",
        "httpbin.org/html serves a simple HTML document.",
    ),
    (
        "https://www.iana.org/help/example-domains",
        "Open IANA's documentation for the reserved example domains.",
        "IANA reserves example.com, example.net, and example.org for documentation.",
    ),
)

_ROUTES: tuple[tuple[tuple[str, ...], tuple[_VisitRow, ...]], ...] = (
    (
        ("example.com", "example domain", "httpbin"),
        (
            _POOL[0],
            _POOL[2],
        ),
    ),
    (
        ("python",),
        (
            (
                "https://www.python.org/",
                "Open the Python language homepage for an official description.",
                "Python is a programming language with an official homepage at python.org.",
            ),
            (
                "https://docs.python.org/3/",
                "Open the Python 3 documentation index.",
                "The Python 3 documentation is published at docs.python.org.",
            ),
        ),
    ),
    (
        ("playwright",),
        (
            (
                "https://playwright.dev/python/",
                "Open the Playwright Python documentation.",
                "Playwright provides a Python library for browser automation.",
            ),
        ),
    ),
    (
        ("docker",),
        (
            (
                "https://docs.docker.com/get-started/",
                "Open the Docker get-started guide.",
                "Docker documents how to build and run containers.",
            ),
        ),
    ),
    (
        ("vultr",),
        (
            (
                "https://www.vultr.com/",
                "Open the Vultr homepage.",
                "Vultr provides cloud infrastructure, including virtual machines.",
            ),
        ),
    ),
    (
        ("wikipedia", "wiki"),
        (
            (
                "https://en.wikipedia.org/wiki/Main_Page",
                "Open the English Wikipedia main page.",
                "The English Wikipedia main page is published at wikipedia.org.",
            ),
        ),
    ),
)

_STUB_NOTE = "Deterministic stub planner; VULTR_INFERENCE_API_KEY is unset."
_FALLBACK_NOTE = "Inference planning failed; fell back to the deterministic stub planner."

_INFERENCE_SYSTEM = """You plan source URLs for a research agent that must screenshot each page.
Reply with JSON only, no markdown, using this shape:
{"visits":[{"url":"https://...","purpose":"...","expected_claim":"..."}]}
Rules:
- 1 to 3 items
- each url is an absolute http or https URL with no credentials
- purpose says why the page is worth opening
- expected_claim is one sentence the page might support
"""


def _normalize(question: str) -> str:
    return re.sub(r"\s+", " ", question.strip().lower())


def _matches(text: str, keyword: str) -> bool:
    if any(char in keyword for char in (" ", ".")):
        return keyword in text
    return re.search(rf"\b{re.escape(keyword)}\b", text) is not None


def _route_rows(question: str) -> tuple[_VisitRow, ...] | None:
    text = _normalize(question)
    for keywords, route in _ROUTES:
        if any(_matches(text, keyword) for keyword in keywords):
            return route
    return None


def stub_can_source(question: str) -> bool:
    """True when the stub has a real page for this question.

    Questions that miss ``_ROUTES`` are not given the hash-fallback URLs by
    ``POST /api/research``. Those fallbacks stay available from ``/api/plan``
    for the D2 planner contract, and they are never turned into claims.
    """
    return _route_rows(question) is not None


def _visits(rows: tuple[_VisitRow, ...] | list[_VisitRow]) -> list[PlannedVisit]:
    return [
        PlannedVisit(url=url, purpose=purpose, expected_claim=expected)
        for url, purpose, expected in rows[:MAX_URLS]
    ]


def _fallback_rows(question: str) -> tuple[_VisitRow, _VisitRow]:
    digest = hashlib.sha256(_normalize(question).encode("utf-8")).digest()
    first = digest[0] % len(_POOL)
    second = digest[1] % len(_POOL)
    if second == first:
        second = (first + 1) % len(_POOL)
    return (_POOL[first], _POOL[second])


def plan_with_stub(question: str, *, note: str = _STUB_NOTE) -> ResearchPlan:
    """Deterministic plan. Does not call the network or read an API key."""
    rows: tuple[_VisitRow, ...] | list[_VisitRow] | None = _route_rows(question)
    if not rows:
        rows = _fallback_rows(question)
    return ResearchPlan(
        question=question.strip(),
        planner=PlannerKind.stub,
        model=None,
        planned_at=datetime.now(timezone.utc),
        visits=_visits(tuple(rows)),
        note=note,
    )


def parse_plan_payload(raw: str) -> list[PlannedVisit]:
    """Parse an Inference completion into planned visits."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("visits"), list):
        raise InferenceError("Inference plan JSON is missing a visits list.")
    visits = [PlannedVisit.model_validate(item) for item in data["visits"][:MAX_URLS]]
    if not visits:
        raise InferenceError("Inference plan did not include any visits.")
    return visits


def plan_with_inference(question: str) -> ResearchPlan:
    """Call Vultr Inference. Only used when a key and model id are set."""
    raw = complete_chat(
        [
            {"role": "system", "content": _INFERENCE_SYSTEM},
            {"role": "user", "content": question.strip()},
        ]
    )
    visits = parse_plan_payload(raw)
    return ResearchPlan(
        question=question.strip(),
        planner=PlannerKind.vultr_inference,
        model=os.getenv("VULTR_INFERENCE_MODEL", "").strip() or None,
        planned_at=datetime.now(timezone.utc),
        visits=visits,
        note=None,
    )


def plan_research(question: str) -> ResearchPlan:
    """Plan URLs for a question. Empty Inference key selects the stub."""
    if not inference_configured():
        return plan_with_stub(question)
    try:
        return plan_with_inference(question)
    except (
        InferenceNotConfigured,
        InferenceError,
        ValidationError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return plan_with_stub(question, note=_FALLBACK_NOTE)
