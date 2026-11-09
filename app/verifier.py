"""Quote verification for screenshot receipts.

Text mode is the default and does not call the network. It accepts a quote
only when that quote appears in the page text captured with the screenshot
(or is the page title). Vision mode runs when ``VULTR_INFERENCE_API_KEY``
and ``VULTR_INFERENCE_MODEL`` are set and ``VERIFICATION_MODE`` is ``auto``
or ``vision``. A vision answer of false drops the claim. A vision error
falls back to the text check so a bad Inference response cannot invent one.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from pathlib import Path

from app.inference import (
    InferenceError,
    InferenceNotConfigured,
    complete_chat,
    inference_configured,
)
from app.schemas import ScreenshotReceipt

log = logging.getLogger("research_with_receipts.verifier")

MAX_VISION_BYTES = 4_000_000

_VISION_SYSTEM = (
    "You check whether a quote is visibly rendered in a screenshot. "
    'Reply with JSON only: {"visible": true} or {"visible": false}. '
    "visible is true only when that quote's words appear in the image. "
    "Do not use outside knowledge."
)

_STOPWORDS = frozenset(
    """
    a an the of to and or for in on at by with from is are was were be been being
    it this that these those what when where who why how does do did doing into
    over as not no yes you your we they their its if then than about which whom
    whose after before during while between within without again further once
    here there all any both each few more most other some such only own same so
    too very can will just should now
    """.split()
)


def verification_backend() -> str:
    """``vision`` when a configured model will be asked; otherwise ``text``."""
    mode = os.getenv("VERIFICATION_MODE", "auto").strip().lower() or "auto"
    if mode not in {"auto", "text", "vision"}:
        mode = "auto"
    if mode == "text":
        return "text"
    model = os.getenv("VULTR_INFERENCE_MODEL", "").strip()
    if mode in {"auto", "vision"} and inference_configured() and model:
        return "vision"
    return "text"


def normalize_text(value: str) -> str:
    text = value.casefold()
    text = (
        text.replace("“", '"')
        .replace("”", '"')
        .replace("’", "'")
        .replace("‘", "'")
        .replace("—", "-")
        .replace("–", "-")
    )
    return " ".join(text.split())


def _quote_needles(quote: str) -> list[str]:
    """Normalized quote, plus the same text without a trailing sentence mark.

    A captured sentence often ends with ``.`` while the stored page text
    continues into the next sentence (``examples. More`` vs ``examples in``).
    The trailing mark is the only character dropped; the words stay intact.
    """
    needle = normalize_text(quote)
    if len(needle) < 4:
        return []
    needles = [needle]
    stripped = needle.rstrip(".,;:!?\"'")
    if stripped != needle and len(stripped) >= 4:
        needles.append(stripped)
    return needles


def quote_in_capture(quote: str, receipt: ScreenshotReceipt) -> bool:
    """True when ``quote`` appears in the captured page text or title."""
    needles = _quote_needles(quote)
    if not needles:
        return False
    page = normalize_text(receipt.page_text or "")
    title = normalize_text(receipt.title or "")
    for needle in needles:
        if page and needle in page:
            return True
        if title and needle in title:
            return True
    return False


def decide_verification(text_ok: bool, vision: bool | None) -> str | None:
    """Combine the text check with an optional vision answer.

    ``vision is False`` rejects the quote even when the DOM text contains it,
    because the words are not on the screenshot. ``None`` means vision did
    not run, so the text check stands.
    """
    if vision is False:
        return None
    if vision is True:
        return "vision"
    if text_ok:
        return "text"
    return None


def significant_terms(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+(?:\.[a-z0-9]+)*", text.casefold())
    return [token for token in tokens if token not in _STOPWORDS and len(token) >= 4]


def longest_term_in_capture(question: str, receipt: ScreenshotReceipt) -> bool:
    """True when the question's longest distinctive term appears in the capture.

    Used for Inference plans so a page that never mentions the subject cannot
    support a claim. Stub routes are selected before this runs.
    """
    terms = significant_terms(question)
    if not terms:
        return False
    longest = max(terms, key=len)
    haystack = normalize_text(
        " ".join(
            part
            for part in (
                receipt.url,
                receipt.title or "",
                receipt.page_text or "",
                receipt.quote or "",
            )
            if part
        )
    )
    return longest in haystack


def numbers_grounded(statement: str, receipt: ScreenshotReceipt) -> bool:
    """Every digit-run in the statement must also appear in the capture."""
    numbers = re.findall(r"\d+", statement)
    if not numbers:
        return True
    haystack = " ".join(
        part
        for part in (
            receipt.url,
            receipt.title or "",
            receipt.page_text or "",
            receipt.quote or "",
        )
        if part
    )
    present = set(re.findall(r"\d+", haystack))
    return all(number in present for number in numbers)


def _coerce_visible(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().casefold().strip(".")
        if token in {"true", "yes", "visible"}:
            return True
        if token in {"false", "no", "not visible", "invisible"}:
            return False
    return None


def parse_visibility(raw: str) -> bool | None:
    """Parse a vision completion. Prose that is not JSON does not count."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, count=1, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and "visible" in data:
            return _coerce_visible(data.get("visible"))
    return _coerce_visible(text)


def vision_quote_visible(image_path: Path, quote: str) -> bool | None:
    """Ask Vultr Inference whether ``quote`` is visible in the PNG.

    Returns True/False from the model, or None when vision is not configured
    or the call cannot be used. None keeps the text decision.
    """
    if verification_backend() != "vision":
        return None
    if not image_path.is_file():
        return None
    try:
        size = image_path.stat().st_size
        if size <= 8 or size > MAX_VISION_BYTES:
            return None
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    except OSError:
        return None
    prompt = (
        "Does the following quote appear visibly in the screenshot? "
        'Reply JSON only: {"visible": true} or {"visible": false}.\n'
        f"Quote:\n{quote[:2000]}"
    )
    try:
        raw = complete_chat(
            [
                {"role": "system", "content": _VISION_SYSTEM},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{encoded}"},
                        },
                    ],
                },
            ],
            temperature=0,
        )
    except (InferenceNotConfigured, InferenceError) as exc:
        log.warning("vision verification unavailable: %s", exc)
        return None
    return parse_visibility(raw)
