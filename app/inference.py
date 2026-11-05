"""Optional Vultr Serverless Inference client.

D2 does not require a key and does not call the network unless
``VULTR_INFERENCE_API_KEY`` is non-empty. With an empty key the planner
uses the deterministic stub. The request matches the OpenAI-compatible
``POST /chat/completions`` shape so a key and model id can be plugged in
later without changing callers.

The API key is read from the environment only. It is never written to
disk, logged, or forwarded into the sandbox.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.vultrinference.com/v1"


class InferenceNotConfigured(RuntimeError):
    """No API key is set. Callers must use the stub planner."""


class InferenceError(RuntimeError):
    """A configured Inference call failed. Callers fall back to the stub."""


def inference_configured() -> bool:
    return bool(os.getenv("VULTR_INFERENCE_API_KEY", "").strip())


def _redact(text: str) -> str:
    key = os.getenv("VULTR_INFERENCE_API_KEY", "").strip()
    if key:
        text = text.replace(key, "[redacted]")
    return text[:300]


def complete_chat(messages: list[dict[str, Any]], *, temperature: float = 0) -> str:
    """Return assistant text from Vultr Serverless Inference.

    Raises InferenceNotConfigured when the key is empty (no HTTP call).
    Raises InferenceError when the key is set but model id is missing or
    the request fails.
    """
    api_key = os.getenv("VULTR_INFERENCE_API_KEY", "").strip()
    if not api_key:
        raise InferenceNotConfigured(
            "VULTR_INFERENCE_API_KEY is empty; Inference is not called."
        )

    model = os.getenv("VULTR_INFERENCE_MODEL", "").strip()
    if not model:
        raise InferenceError(
            "VULTR_INFERENCE_MODEL is empty. Set a model id before using Inference."
        )

    base = os.getenv("VULTR_INFERENCE_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")
    if not base:
        base = DEFAULT_BASE_URL
    timeout = float(os.getenv("VULTR_INFERENCE_TIMEOUT_SEC", "30"))
    payload: dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "messages": messages,
    }
    try:
        response = httpx.post(
            f"{base}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        body = response.json()
        content = body["choices"][0]["message"]["content"]
    except InferenceError:
        raise
    except Exception as exc:
        raise InferenceError(f"Vultr Inference request failed: {_redact(str(exc))}") from exc

    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        content = "".join(parts)
    if not isinstance(content, str) or not content.strip():
        raise InferenceError("Vultr Inference returned an empty completion.")
    return content
