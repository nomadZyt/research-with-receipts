"""D2 contracts: stub planner, claim schema, receipt publishing. No Inference key."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.inference import InferenceError, InferenceNotConfigured, complete_chat
from app.main import app
from app.orchestrator import assemble_claims, run_research
from app.planner import parse_plan_payload, plan_research, plan_with_inference, plan_with_stub
from app.sandbox_runner import PNG_MAGIC, dispatch_sandbox, publish_receipts, screenshots_dir, translate_to_docker_host_path
from app.schemas import Claim, PlannerKind, ResearchPlan, ScreenshotReceipt
from sandbox.worker import check_urls


def _valid_claim(**overrides: object) -> dict[str, object]:
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
        "verdict": "pending_vision_check",
    }
    payload.update(overrides)
    return payload


class D2ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._saved: dict[str, str | None] = {}
        updates = {
            "VULTR_INFERENCE_API_KEY": "",
            "VULTR_INFERENCE_MODEL": "",
            "SCREENSHOTS_DIR": str(Path(self.tmp.name) / "screenshots"),
            "SANDBOX_JOBS_DIR": str(Path(self.tmp.name) / "jobs"),
            "HOST_SANDBOX_JOBS_DIR": "",
        }
        for key, value in updates.items():
            self._saved[key] = os.environ.get(key)
            os.environ[key] = value
        self.client = TestClient(app)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_health_reports_stub_without_key(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["inference"], "stub")

    def test_stub_planner_is_deterministic_and_not_example_com_only(self) -> None:
        questions = [
            "hello",
            "When was the library founded?",
            "What does example.com say it is for?",
            "How does Python handle packaging?",
        ]
        for question in questions:
            first = plan_with_stub(question)
            second = plan_with_stub(question)
            urls = [visit.url for visit in first.visits]
            self.assertEqual(urls, [visit.url for visit in second.visits])
            self.assertNotEqual(urls, ["https://example.com/"])
            self.assertTrue(any("example.com" not in url for url in urls))
            self.assertEqual(first.planner, PlannerKind.stub)
        example = [visit.url for visit in plan_with_stub("What does example.com say it is for?").visits]
        self.assertIn("https://example.com/", example)
        self.assertIn("https://httpbin.org/html", example)
        python_urls = [visit.url for visit in plan_with_stub("How does Python handle packaging?").visits]
        self.assertIn("https://www.python.org/", python_urls)
        self.assertIn("https://docs.python.org/3/", python_urls)
        plain = plan_with_stub("When was the library founded?")
        self.assertEqual(len(plain.visits), 2)

    def test_plan_endpoint_does_not_require_inference(self) -> None:
        response = self.client.post(
            "/api/plan",
            json={"question": "What does example.com say it is for?"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["planner"], "stub")
        self.assertGreaterEqual(len(body["visits"]), 2)
        self.assertTrue(body["visits"][0]["expected_claim"])

    def test_inference_client_makes_no_request_without_key(self) -> None:
        with patch("app.inference.httpx.post", side_effect=AssertionError("network")):
            with self.assertRaises(InferenceNotConfigured):
                complete_chat([{"role": "user", "content": "hi"}])
        plan = plan_research("What does example.com say it is for?")
        self.assertEqual(plan.planner, PlannerKind.stub)

    def test_inference_plan_shape_and_fallback(self) -> None:
        payload = json.dumps(
            {
                "visits": [
                    {
                        "url": "https://example.com/",
                        "purpose": "Read the reserved domain page.",
                        "expected_claim": "example.com is reserved for examples.",
                    }
                ]
            }
        )
        os.environ["VULTR_INFERENCE_API_KEY"] = "not-a-real-key"
        os.environ["VULTR_INFERENCE_MODEL"] = "demo-model"
        with patch("app.planner.complete_chat", return_value=f"```json\n{payload}\n```"):
            plan = plan_with_inference("reserved domains")
        self.assertEqual(plan.planner, PlannerKind.vultr_inference)
        self.assertEqual(plan.model, "demo-model")
        self.assertEqual(str(plan.visits[0].url), "https://example.com/")

        with patch("app.planner.complete_chat", side_effect=InferenceError("down")):
            fallen = plan_research("When was the library founded?")
        self.assertEqual(fallen.planner, PlannerKind.stub)
        self.assertIn("fell back", fallen.note or "")
        visits = parse_plan_payload(payload)
        self.assertEqual(len(visits), 1)

    def test_claim_schema_requires_receipt_fields(self) -> None:
        ok = self.client.post("/api/claims/validate", json=_valid_claim())
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["verdict"], "pending_vision_check")

        missing = _valid_claim()
        del missing["screenshot_path"]
        self.assertEqual(self.client.post("/api/claims/validate", json=missing).status_code, 422)

        for bad in (
            _valid_claim(url="file:///etc/passwd"),
            _valid_claim(url="javascript:alert(1)"),
            _valid_claim(url="https://user:secret@example.com/"),
            _valid_claim(screenshot_path="/etc/passwd"),
            _valid_claim(screenshot_path="screenshots/../../etc/passwd.png"),
            _valid_claim(quote=""),
            _valid_claim(claim=""),
        ):
            response = self.client.post("/api/claims/validate", json=bad)
            self.assertEqual(response.status_code, 422, bad)

        coerced = Claim.model_validate(
            _valid_claim(
                captured_at="2026-11-04T03:00:00",
                created_at="2026-11-04T03:00:01",
            )
        )
        self.assertIsNotNone(coerced.captured_at.tzinfo)
        self.assertIsNotNone(coerced.created_at.tzinfo)

    def test_sandbox_request_rejects_non_http_urls(self) -> None:
        response = self.client.post(
            "/api/sandbox/run",
            json={"urls": ["file:///tmp/x", "https://example.com/"]},
        )
        self.assertEqual(response.status_code, 422)
        with self.assertRaises(ValueError):
            check_urls(["javascript:alert(1)"])
        self.assertEqual(check_urls(["https://example.com/"]), ["https://example.com/"])

    def test_publish_receipts_and_assemble_claims(self) -> None:
        out = Path(self.tmp.name) / "out"
        out.mkdir()
        (out / "0.png").write_bytes(PNG_MAGIC + b"png-bytes")
        (out / "0.json").write_text(
            json.dumps(
                {
                    "url": "https://example.com/",
                    "title": "Example Domain",
                    "quote": "This domain is for use in illustrative examples.",
                    "captured_at": "2026-11-04T03:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        (out / "1.png").write_bytes(b"not-a-png")
        (out / "1.json").write_text(
            json.dumps({"url": "https://httpbin.org/html", "error": "navigation failed"}),
            encoding="utf-8",
        )
        raw = publish_receipts(
            sandbox_id="rwr-aaaaaaaaaaaa",
            out_dir=out,
            urls=["https://example.com/", "https://httpbin.org/html"],
        )
        self.assertTrue(raw[0]["ok"])
        self.assertFalse(raw[1]["ok"])
        self.assertTrue((screenshots_dir() / "rwr-aaaaaaaaaaaa" / "0.png").is_file())
        receipts = [ScreenshotReceipt.model_validate(item) for item in raw]
        plan = ResearchPlan.model_validate(
            {
                "question": "What does example.com say it is for?",
                "planner": "stub",
                "planned_at": datetime.now().isoformat(),
                "visits": [
                    {
                        "url": "https://example.com/",
                        "purpose": "Capture example.com.",
                        "expected_claim": "example.com is reserved for examples.",
                    },
                    {
                        "url": "https://httpbin.org/html",
                        "purpose": "Capture httpbin.",
                        "expected_claim": "httpbin serves HTML.",
                    },
                ],
            }
        )
        claims = assemble_claims(plan, receipts)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].claim, "example.com is reserved for examples.")
        self.assertEqual(claims[0].screenshot_path, "screenshots/rwr-aaaaaaaaaaaa/0.png")
        self.assertTrue(claims[0].captured_at.tzinfo)

    def test_research_refuses_without_receipts_and_accepts_real_ones(self) -> None:
        def skipped(urls: list[str]) -> dict[str, object]:
            return {
                "sandbox_id": "rwr-0123456789ab",
                "destroyed": True,
                "skipped": True,
                "receipts": [],
                "refusal": "Docker is not available on this host; sandbox was not started.",
            }

        with patch("app.orchestrator.dispatch_sandbox", side_effect=skipped):
            refused = self.client.post(
                "/api/research",
                json={"question": "What does example.com say it is for?"},
            )
        self.assertEqual(refused.status_code, 200)
        body = refused.json()
        self.assertEqual(body["status"], "refused")
        self.assertEqual(body["claims"], [])
        self.assertEqual(body["planner"], "stub")
        self.assertGreaterEqual(len(body["visits"]), 2)
        self.assertIn("Docker is not available", body["refusal"])

        def published(urls: list[str]) -> dict[str, object]:
            sandbox_id = "rwr-bbbbbbbbbbbb"
            dest = screenshots_dir() / sandbox_id
            dest.mkdir(parents=True, exist_ok=True)
            png = dest / "0.png"
            png.write_bytes(PNG_MAGIC + b"png-bytes")
            return {
                "sandbox_id": sandbox_id,
                "destroyed": True,
                "skipped": False,
                "refusal": None,
                "receipts": [
                    {
                        "screenshot_id": f"{sandbox_id}-0",
                        "sandbox_id": sandbox_id,
                        "index": 0,
                        "url": urls[0],
                        "relative_path": f"screenshots/{sandbox_id}/0.png",
                        "title": "Example Domain",
                        "quote": "This domain is for use in illustrative examples.",
                        "captured_at": "2026-11-04T03:00:00+00:00",
                        "byte_size": png.stat().st_size,
                        "ok": True,
                        "error": None,
                    }
                ],
            }

        with patch("app.orchestrator.dispatch_sandbox", side_effect=published):
            accepted = run_research("What does example.com say it is for?")
        self.assertEqual(accepted.status, "ok")
        self.assertEqual(len(accepted.claims), 1)
        self.assertTrue(accepted.destroyed)
        self.assertEqual(accepted.claims[0].verdict.value, "pending_vision_check")
        shot = self.client.get(f"/api/screenshots/{accepted.sandbox_id}/0.png")
        self.assertEqual(shot.status_code, 200)
        self.assertEqual(shot.headers["content-type"], "image/png")
        missing = self.client.get("/api/screenshots/not-an-id/0.png")
        self.assertEqual(missing.status_code, 404)

    def test_dispatch_skips_when_docker_missing(self) -> None:
        with patch("app.sandbox_runner._docker_available", return_value=False):
            result = dispatch_sandbox(["https://example.com/"])
        self.assertTrue(result["skipped"])
        self.assertTrue(result["destroyed"])
        self.assertEqual(result["receipts"], [])

    def test_translate_bind_mount_and_explicit_host_path(self) -> None:
        with patch(
            "app.sandbox_runner._mount_entries",
            return_value=[
                ("/", "/"),
                ("/sandbox-jobs", "/home/dev/repo/var/sandbox-jobs"),
            ],
        ):
            os.environ["HOST_SANDBOX_JOBS_DIR"] = ""
            translated = translate_to_docker_host_path(Path("/sandbox-jobs/rwr-abc"))
        self.assertEqual(translated, Path("/home/dev/repo/var/sandbox-jobs/rwr-abc"))

        job = Path(os.environ["SANDBOX_JOBS_DIR"]) / "rwr-abc"
        job.mkdir(parents=True)
        os.environ["HOST_SANDBOX_JOBS_DIR"] = "/host/jobs"
        self.assertEqual(
            translate_to_docker_host_path(job),
            Path("/host/jobs/rwr-abc"),
        )


if __name__ == "__main__":
    unittest.main()
