"""D3: quote verification, refusal, and sandbox secret whitelist. No Inference key."""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.orchestrator import assemble_claims, run_research
from app.sandbox_runner import (
    PNG_MAGIC,
    SANDBOX_ENV_WHITELIST,
    build_docker_command,
    container_env_flags,
    dispatch_sandbox,
    docker_cli_env,
    resolve_screenshot,
    screenshots_dir,
)
from app.schemas import (
    Claim,
    PlannerKind,
    PlannedVisit,
    ResearchPlan,
    ResearchResponse,
    ScreenshotReceipt,
)
from app.verifier import decide_verification, parse_visibility, verification_backend
from sandbox.worker import scrub_process_env


def _claim(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "claim_id": "clm-0123456789ab",
        "claim": "example.com is reserved for documentation.",
        "url": "https://example.com/",
        "quote": "This domain is for use in illustrative examples.",
        "screenshot_id": "rwr-0123456789ab-0",
        "screenshot_path": "screenshots/rwr-0123456789ab/0.png",
        "captured_at": "2026-11-04T03:00:00Z",
        "created_at": "2026-11-04T03:00:01Z",
        "source_title": "Example Domain",
        "verdict": "supported",
        "verifier": "text",
    }
    payload.update(overrides)
    return payload


class D3VerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile_dir()
        self._saved: dict[str, str | None] = {}
        updates = {
            "VULTR_INFERENCE_API_KEY": "",
            "VULTR_INFERENCE_MODEL": "",
            "VERIFICATION_MODE": "",
            "SCREENSHOTS_DIR": str(Path(self.tmp) / "screenshots"),
            "SANDBOX_JOBS_DIR": str(Path(self.tmp) / "jobs"),
            "HOST_SANDBOX_JOBS_DIR": "",
        }
        for key, value in updates.items():
            self._saved[key] = os.environ.get(key)
            os.environ[key] = value
        self.client = TestClient(app)
        self._extra_env: list[str] = []

    def tearDown(self) -> None:
        for key in self._extra_env:
            os.environ.pop(key, None)
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        Path(self.tmp).chmod(0o700)
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _set_env(self, key: str, value: str) -> None:
        if key not in self._saved and key not in self._extra_env:
            self._extra_env.append(key)
        os.environ[key] = value

    def _write_receipt(
        self,
        sandbox_id: str,
        *,
        url: str,
        quote: str,
        page_text: str,
        title: str = "Example Domain",
        ok: bool = True,
        png: bytes | None = None,
    ) -> dict[str, object]:
        dest = screenshots_dir() / sandbox_id
        dest.mkdir(parents=True, exist_ok=True)
        blob = PNG_MAGIC + b"png-bytes" if png is None else png
        image = dest / "0.png"
        image.write_bytes(blob)
        return {
            "screenshot_id": f"{sandbox_id}-0",
            "sandbox_id": sandbox_id,
            "index": 0,
            "url": url,
            "relative_path": f"screenshots/{sandbox_id}/0.png",
            "title": title,
            "quote": quote,
            "page_text": page_text,
            "captured_at": "2026-11-04T03:00:00+00:00",
            "byte_size": image.stat().st_size,
            "ok": ok,
            "error": None,
        }

    def _sandbox(self, urls: list[str], receipt: dict[str, object]) -> dict[str, object]:
        return {
            "sandbox_id": receipt["sandbox_id"],
            "destroyed": True,
            "skipped": False,
            "refusal": None,
            "receipts": [receipt],
        }

    def test_health_uses_text_verification_without_a_key(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["inference"], "stub")
        self.assertEqual(body["verification"], "text")
        self.assertEqual(verification_backend(), "text")

    def test_pending_vision_check_is_rejected(self) -> None:
        missing_verdict = _claim()
        del missing_verdict["verdict"]
        del missing_verdict["verifier"]
        omitted = self.client.post("/api/claims/validate", json=missing_verdict)
        self.assertEqual(omitted.status_code, 200, omitted.text)
        self.assertEqual(omitted.json()["verdict"], "unsupported")
        self.assertIsNone(omitted.json()["verifier"])

        pending = self.client.post(
            "/api/claims/validate",
            json=_claim(verdict="pending_vision_check"),
        )
        self.assertEqual(pending.status_code, 422)

    def test_response_schema_refuses_unverified_or_nonempty_refusal(self) -> None:
        refused = {
            "status": "refused",
            "question": "When was the city of Zorblax founded?",
            "planner": "stub",
            "planned_at": "2026-11-05T00:00:00Z",
            "visits": [],
            "claims": [],
            "refusal": "No supported source is available for this question.",
            "refusal_code": "unsourceable",
        }
        ResearchResponse.model_validate(refused)
        with self.assertRaises(ValidationError):
            ResearchResponse.model_validate({**refused, "claims": [_claim()]})
        with self.assertRaises(ValidationError):
            ResearchResponse.model_validate({**refused, "refusal": None})
        with self.assertRaises(ValidationError):
            ResearchResponse.model_validate(
                {
                    **refused,
                    "status": "ok",
                    "refusal": None,
                    "refusal_code": None,
                    "claims": [],
                }
            )
        supported = _claim()
        del supported["verifier"]
        with self.assertRaises(ValidationError):
            ResearchResponse.model_validate(
                {
                    **refused,
                    "status": "ok",
                    "refusal": None,
                    "refusal_code": None,
                    "claims": [supported],
                }
            )
        ResearchResponse.model_validate(
            {
                **refused,
                "status": "ok",
                "refusal": None,
                "refusal_code": None,
                "verification": "text",
                "claims": [_claim()],
            }
        )

    def test_text_verification_keeps_only_quotes_present_in_the_capture(self) -> None:
        quote = "This domain is for use in illustrative examples."
        page = "Example Domain " + quote + " You may use this domain in literature."
        sandbox_id = "rwr-aaaaaaaaaaaa"
        good = self._write_receipt(
            sandbox_id,
            url="https://example.com/",
            quote=quote,
            page_text=page,
        )
        plan = _example_plan()
        claims = assemble_claims(plan, [ScreenshotReceipt.model_validate(good)])
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].verdict.value, "supported")
        self.assertEqual(claims[0].verifier.value, "text")
        self.assertIn(claims[0].quote, page)
        self.assertTrue(resolve_screenshot(claims[0].screenshot_path).is_file())

        mismatch = self._write_receipt(
            "rwr-bbbbbbbbbbbb",
            url="https://example.com/",
            quote="Zorblax was founded in the year twelve.",
            page_text=page,
        )
        self.assertEqual(
            assemble_claims(plan, [ScreenshotReceipt.model_validate(mismatch)]),
            [],
        )

        not_png = self._write_receipt(
            "rwr-cccccccccccc",
            url="https://example.com/",
            quote=quote,
            page_text=page,
            png=b"this is not a png",
        )
        self.assertEqual(
            assemble_claims(plan, [ScreenshotReceipt.model_validate(not_png)]),
            [],
        )

    def test_digit_not_in_the_capture_drops_the_claim(self) -> None:
        quote = "This domain is for use in illustrative examples."
        receipt = ScreenshotReceipt.model_validate(
            self._write_receipt(
                "rwr-dddddddddddd",
                url="https://example.com/",
                quote=quote,
                page_text=quote,
            )
        )
        plan = _example_plan(expected_claim="example.com has been reserved since 1999.")
        self.assertEqual(assemble_claims(plan, [receipt]), [])
        grounded = _example_plan(expected_claim="example.com is reserved for examples.")
        self.assertEqual(len(assemble_claims(grounded, [receipt])), 1)

    def test_inference_plan_cannot_cite_an_unrelated_page(self) -> None:
        quote = "This domain is for use in illustrative examples."
        receipt = ScreenshotReceipt.model_validate(
            self._write_receipt(
                "rwr-eeeeeeeeeeee",
                url="https://example.com/",
                quote=quote,
                page_text=quote,
            )
        )
        fictional = ResearchPlan(
            question="When was the city of Zorblax founded?",
            planner=PlannerKind.vultr_inference,
            model="demo-model",
            planned_at=datetime.now(timezone.utc),
            visits=[
                PlannedVisit(
                    url="https://example.com/",
                    purpose="Search for the city.",
                    expected_claim="Zorblax was founded in 1847.",
                )
            ],
        )
        self.assertEqual(assemble_claims(fictional, [receipt]), [])

        related = ResearchPlan(
            question="What does example.com say it is for?",
            planner=PlannerKind.vultr_inference,
            model="demo-model",
            planned_at=datetime.now(timezone.utc),
            visits=[
                PlannedVisit(
                    url="https://example.com/",
                    purpose="Read the reserved domain page.",
                    expected_claim="example.com is reserved for illustrative examples.",
                )
            ],
        )
        claims = assemble_claims(related, [receipt])
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].verdict.value, "supported")

    def test_fictional_question_refuses_without_sandbox_or_claims(self) -> None:
        question = "When was the city of Zorblax founded?"
        with patch(
            "app.orchestrator.dispatch_sandbox",
            side_effect=AssertionError("sandbox must not run"),
        ):
            with patch("app.inference.httpx.post", side_effect=AssertionError("network")):
                first = self.client.post("/api/research", json={"question": question})
                second = self.client.post("/api/research", json={"question": question})
        for response in (first, second):
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(body["status"], "refused")
            self.assertEqual(body["claims"], [])
            self.assertEqual(body["visits"], [])
            self.assertEqual(body["receipts"], [])
            self.assertEqual(body["refusal_code"], "unsourceable")
            self.assertIn("invent", body["refusal"].lower())
            self.assertIsNone(body["sandbox_id"])
            self.assertFalse(body["destroyed"])
            leaked = json.dumps(
                {"visits": body["visits"], "claims": body["claims"], "receipts": body["receipts"]}
            )
            self.assertNotIn("http", leaked)
        self.assertEqual(first.json()["refusal"], second.json()["refusal"])

        library = self.client.post(
            "/api/research",
            json={"question": "When was the library founded?"},
        )
        self.assertEqual(library.json()["status"], "refused")
        self.assertEqual(library.json()["claims"], [])
        self.assertEqual(library.json()["refusal_code"], "unsourceable")

        planned = self.client.post("/api/plan", json={"question": question})
        self.assertGreaterEqual(len(planned.json()["visits"]), 1)

    def test_sourceable_question_refuses_when_the_quote_is_not_in_the_page(self) -> None:
        def published(urls: list[str]) -> dict[str, object]:
            receipt = self._write_receipt(
                "rwr-ffffffffffff",
                url=urls[0],
                quote="Totally different words that are not on the page.",
                page_text="Example Domain This domain is for use in illustrative examples.",
            )
            return self._sandbox(urls, receipt)

        with patch("app.orchestrator.dispatch_sandbox", side_effect=published):
            with patch("app.inference.httpx.post", side_effect=AssertionError("network")):
                response = self.client.post(
                    "/api/research",
                    json={"question": "What does example.com say it is for?"},
                )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "refused")
        self.assertEqual(body["claims"], [])
        self.assertEqual(body["refusal_code"], "unverified")
        self.assertTrue(body["refusal"])
        self.assertTrue(body["destroyed"])
        self.assertTrue(body["sandbox_id"])
        self.assertEqual(body["verification"], "text")

    def test_sourceable_question_returns_only_verified_png_claims(self) -> None:
        quote = "This domain is for use in illustrative examples."

        def published(urls: list[str]) -> dict[str, object]:
            receipt = self._write_receipt(
                "rwr-121212121212",
                url=urls[0],
                quote=quote,
                page_text="Example Domain " + quote + " in documents.",
            )
            return self._sandbox(urls, receipt)

        with patch("app.orchestrator.dispatch_sandbox", side_effect=published):
            with patch("app.inference.httpx.post", side_effect=AssertionError("network")):
                response = self.client.post(
                    "/api/research",
                    json={"question": "What does example.com say it is for?"},
                )
        body = response.json()
        self.assertEqual(body["status"], "ok", response.text)
        self.assertEqual(len(body["claims"]), 1)
        claim = body["claims"][0]
        self.assertEqual(claim["verdict"], "supported")
        self.assertEqual(claim["verifier"], "text")
        self.assertIn(claim["quote"], body["receipts"][0]["page_text"])
        image = resolve_screenshot(claim["screenshot_path"])
        self.assertTrue(image.is_file())
        self.assertEqual(image.read_bytes()[:8], PNG_MAGIC)
        self.assertIsNone(body["refusal"])
        self.assertTrue(body["destroyed"])

    def test_vision_false_drops_a_quote_that_text_would_accept(self) -> None:
        quote = "This domain is for use in illustrative examples."
        self._set_env("VULTR_INFERENCE_API_KEY", "test-key-not-real")
        self._set_env("VULTR_INFERENCE_MODEL", "demo-vision")

        def published(urls: list[str]) -> dict[str, object]:
            receipt = self._write_receipt(
                "rwr-131313131313",
                url=urls[0],
                quote=quote,
                page_text="Example Domain " + quote,
            )
            return self._sandbox(urls, receipt)

        with patch("app.orchestrator.dispatch_sandbox", side_effect=published):
            with patch("app.inference.httpx.post", side_effect=RuntimeError("no network")):
                with patch(
                    "app.verifier.complete_chat",
                    return_value='{"visible": false}',
                ) as vision:
                    refused = run_research("What does example.com say it is for?")
        vision.assert_called()
        self.assertEqual(refused.status, "refused")
        self.assertEqual(refused.claims, [])
        self.assertEqual(refused.refusal_code, "unverified")

        with patch("app.orchestrator.dispatch_sandbox", side_effect=published):
            with patch("app.inference.httpx.post", side_effect=RuntimeError("no network")):
                with patch(
                    "app.verifier.complete_chat",
                    return_value='```json\n{"visible": true}\n```',
                ):
                    accepted = run_research("What does example.com say it is for?")
        self.assertEqual(accepted.status, "ok")
        self.assertEqual(len(accepted.claims), 1)
        self.assertEqual(accepted.claims[0].verifier.value, "vision")
        self.assertEqual(accepted.verification, "vision")

    def test_text_mode_does_not_call_vision_even_if_a_key_is_set(self) -> None:
        quote = "This domain is for use in illustrative examples."
        self._set_env("VULTR_INFERENCE_API_KEY", "test-key-not-real")
        self._set_env("VULTR_INFERENCE_MODEL", "demo-vision")
        self._set_env("VERIFICATION_MODE", "text")

        def published(urls: list[str]) -> dict[str, object]:
            receipt = self._write_receipt(
                "rwr-141414141414",
                url=urls[0],
                quote=quote,
                page_text=quote,
            )
            return self._sandbox(urls, receipt)

        with patch("app.orchestrator.dispatch_sandbox", side_effect=published):
            with patch("app.inference.httpx.post", side_effect=RuntimeError("no network")):
                with patch(
                    "app.verifier.complete_chat",
                    side_effect=AssertionError("vision must not run"),
                ):
                    result = run_research("What does example.com say it is for?")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.claims[0].verifier.value, "text")
        self.assertEqual(verification_backend(), "text")

    def test_visibility_parser_and_decision_table(self) -> None:
        self.assertTrue(parse_visibility('{"visible": true}'))
        self.assertFalse(parse_visibility('```json\n{"visible": false}\n```'))
        self.assertIsNone(parse_visibility("I think the quote is probably there."))
        self.assertEqual(decide_verification(True, None), "text")
        self.assertIsNone(decide_verification(True, False))
        self.assertEqual(decide_verification(False, True), "vision")
        self.assertIsNone(decide_verification(False, None))

    def test_sandbox_whitelist_never_receives_the_inference_key(self) -> None:
        secret = "super-secret-value"
        self._set_env("VULTR_INFERENCE_API_KEY", secret)
        self._set_env("LEAKED_COPY", secret)
        previous_home = os.environ.get("HOME")
        os.environ["HOME"] = "/home/attacker"
        try:
            assigned = _env_assignments(container_env_flags())
            self.assertEqual(assigned, dict(SANDBOX_ENV_WHITELIST))
            blob = " ".join(container_env_flags())
            self.assertNotIn(secret, blob)
            self.assertNotIn("VULTR_INFERENCE_API_KEY", blob)
            self.assertEqual(assigned["HOME"], "/tmp")

            cli = docker_cli_env()
            self.assertNotIn("VULTR_INFERENCE_API_KEY", cli)
            self.assertNotIn("LEAKED_COPY", cli)
            self.assertNotIn(secret, " ".join(cli.values()))

            cmd = build_docker_command(
                sandbox_id="rwr-151515151515",
                host_job=Path("/tmp/job"),
                settings={
                    "image": "research-with-receipts-sandbox:latest",
                    "memory_mb": 128,
                    "cpus": "1",
                    "network": "none",
                    "shm": "64m",
                },
            )
            self.assertNotIn("--env-file", cmd)
            self.assertNotIn(secret, " ".join(cmd))
            self.assertIn("--read-only", cmd)
            self.assertNotIn("VULTR_INFERENCE_API_KEY", cmd)
        finally:
            if previous_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = previous_home

        with patch("app.sandbox_runner._docker_available", return_value=True):
            with patch(
                "app.sandbox_runner.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
            ) as run:
                dispatch_sandbox(["https://example.com/"])
        passed_env = run.call_args.kwargs["env"]
        command = " ".join(run.call_args.args[0])
        self.assertNotIn("VULTR_INFERENCE_API_KEY", passed_env)
        self.assertNotIn(secret, command)
        self.assertNotIn("--env-file", run.call_args.args[0])

    def test_worker_scrubs_secret_env(self) -> None:
        env = {
            "VULTR_INFERENCE_API_KEY": "super-secret-value",
            "HOME": "/tmp",
            "PLAYWRIGHT_BROWSERS_PATH": "/ms-playwright",
            "AWS_SECRET_ACCESS_KEY": "abc",
        }
        removed = scrub_process_env(env)
        self.assertIn("VULTR_INFERENCE_API_KEY", removed)
        self.assertNotIn("VULTR_INFERENCE_API_KEY", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertEqual(env["HOME"], "/tmp")
        self.assertEqual(env["PLAYWRIGHT_BROWSERS_PATH"], "/ms-playwright")

    def test_compose_sandbox_service_has_no_inference_key(self) -> None:
        text = Path("docker-compose.yml").read_text(encoding="utf-8")
        _, sandbox = text.split("\n  sandbox:\n", 1)
        self.assertNotIn("VULTR_INFERENCE_API_KEY", sandbox)


def tempfile_dir() -> str:
    import tempfile

    return tempfile.mkdtemp(prefix="rwr-d3-")


def _env_assignments(flags: list[str]) -> dict[str, str]:
    assigned: dict[str, str] = {}
    index = 0
    while index < len(flags):
        if flags[index] == "-e":
            key, value = flags[index + 1].split("=", 1)
            assigned[key] = value
            index += 2
        else:
            index += 1
    return assigned


def _example_plan(expected_claim: str = "example.com is reserved for examples.") -> ResearchPlan:
    return ResearchPlan(
        question="What does example.com say it is for?",
        planner=PlannerKind.stub,
        planned_at=datetime.now(timezone.utc),
        visits=[
            PlannedVisit(
                url="https://example.com/",
                purpose="Read the reserved domain page.",
                expected_claim=expected_claim,
            )
        ],
    )


class D3ClaimImportTests(unittest.TestCase):
    """Claim objects constructed outside a research response stay schema-only."""

    def test_supported_claim_round_trip(self) -> None:
        claim = Claim.model_validate(_claim())
        self.assertEqual(claim.verdict.value, "supported")
        self.assertEqual(claim.verifier.value, "text")


if __name__ == "__main__":
    unittest.main()
