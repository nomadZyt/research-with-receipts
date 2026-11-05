# Research with Receipts / 带收据的调研

**One-liner:** Verifiable research briefs — every claim ships with a source screenshot and quote, all browsing sandboxed on Vultr; no receipt, no answer.

**一句话：** 带收据的调研 Agent：每个结论必须挂上来源页截图与原文摘录；检索与截图全在 Vultr 沙箱完成；没有凭据就拒答；宿主进程零执行、零写入。

**Track / 赛道:** Blast Radius Zero · Pattern B (sandboxed browser)  
**Hackathon:** [Vultr Agent Rush](https://lablab.ai/ai-hackathons/vultr-hackathon) (2026-11-03 → 11-08)

---

## Pain point / 痛点

Research answers look polished but are unverifiable — chat agents invent sources.  
调研结论漂亮但没法追责——聊天型 Agent 会编造来源。

This product enforces **no receipt → no answer**, makes accountability the default, and keeps browsing blast radius at zero via an isolated sandbox.  
本产品强制「无收据不输出」，把可追责变成默认行为，并用隔离沙箱把浏览动作的 blast radius 压到 0。

---

## Architecture / 架构

```
User question
    ↓
Vultr VM orchestrator (FastAPI)
    ↓ plan via Vultr Serverless Inference
Docker Playwright sandbox (throwaway container)
    ↓ screenshots + page text
Claims with screenshot + quote
    ↓ verify quote against the capture (text, or vision when Inference is configured)
    ↓ drop anything that fails; if none remain → refuse
Response {status, claims[], refusal, refusal_code, sandbox_id, destroyed}
```

Flow:

1. **Orchestrator** (on Vultr VM) receives `{question}` and plans URLs. With `VULTR_INFERENCE_API_KEY` empty, a deterministic **stub planner** chooses the URLs (D2 default). When a key and `VULTR_INFERENCE_MODEL` are both set later, planning goes through **Vultr Serverless Inference** and falls back to the stub if that call fails.
2. **Sandbox** — Playwright Chromium in a short-lived Docker container visits each URL, writes a full-page PNG, quote, and timestamp under `/job/out`.
3. Orchestrator **copies PNGs** to `screenshots/<sandbox_id>/<index>.png` and **verifies** each quote against the captured page text (or a vision model when Inference is configured). A claim is returned only when that check passes and the PNG is on disk. Otherwise **refusal** — no invented sources, no `pending_vision_check`.
4. Container is **destroyed** after the job (`docker run --rm`). The sandbox receives a fixed env whitelist, never `VULTR_INFERENCE_API_KEY`.

Containment focus / 遏制重点:

| Requirement | Practice |
|-------------|----------|
| Process isolation | Playwright only inside container; app receives screenshots/text only |
| Secret hygiene | Sandbox env is a fixed whitelist (`HOME`, `XDG_*`, `PLAYWRIGHT_BROWSERS_PATH`). `VULTR_INFERENCE_API_KEY` stays on the orchestrator |
| Resource limits | Per-job time / memory / CPU caps |
| Lifecycle | Destroy after task; log `sandbox_id` |

---

## Stack / 技术栈

- **Vultr VM** — FastAPI orchestrator (`app/`)
- **Vultr Serverless Inference** — `https://api.vultrinference.com/v1` (optional planning and vision quote-check)
- **Docker + Playwright** — Pattern B sandboxed browser (`docker/sandbox`, `sandbox/worker.py`)
- **Python** — FastAPI, uvicorn, pydantic, httpx, python-dotenv

---

## Demo script / Demo 脚本

Record these three beats (containment moment is required for judging):

1. **Happy path** — Ask a factual question (e.g. “When did product X ship?”). Brief returns; each claim opens URL + screenshot + quote.
2. **Refuse** — Ask about a fictional event (for example “When was the city of Zorblax founded?”). The stub does not attach unrelated pages. The API returns `status: refused`, `refusal_code: unsourceable`, and `claims: []`.
3. **Containment moment** — Show sandbox cannot write host paths / does not receive inference API key; destroy receipt (`destroyed: true`, `sandbox_id`) on screen. See `scripts/demo_containment.sh`.

---

## Setup / 本地启动

### Prerequisites

- Python 3.11+
- Docker (for sandbox builds / runs)
- Vultr Serverless Inference API key (placeholders only in this repo — do not commit secrets)

### Env vars

Copy `.env.example` → `.env` and fill placeholders:

| Variable | Required | Notes |
|----------|----------|--------|
| `VULTR_INFERENCE_API_KEY` | No for D2 | Leave empty. Orchestrator only; never inject into sandbox |
| `VULTR_INFERENCE_BASE_URL` | Default | `https://api.vultrinference.com/v1` |
| `VULTR_INFERENCE_MODEL` | No for the stub path | Leave empty. Read only when the API key is set |
| `VERIFICATION_MODE` | Default `auto` | `auto` uses vision when the key and model are set, otherwise text. `text` never sends screenshots to Inference |
| `VULTR_API_KEY` | Optional | Cloud/API management if you automate VM/sandbox infra |
| `SANDBOX_TIMEOUT_SEC` | Default `120` | Hard timeout budget for sandbox jobs |
| `SANDBOX_MEMORY_MB` | Default `1024` | `docker run --memory` |
| `SANDBOX_SHM_SIZE` | Default `256m` | Chromium `/dev/shm` |
| `HOST_SANDBOX_JOBS_DIR` | Usually empty | Override host path of `var/sandbox-jobs` if Compose mount detection fails |
| `PORT` | Default `8080` | Orchestrator listen port |

### Install & run orchestrator

```bash
cd /path/to/research-with-receipts
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit placeholders — do not invent credentials
uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}"
```

Health: `GET /` (`inference` is `stub` while the API key is empty).

The orchestrator spawns `docker run --rm ...` (see `app/sandbox_runner.py`). The process that runs uvicorn must be able to talk to the Docker daemon (`docker` on `PATH`, user in the `docker` group).

---

## D2 local path / 本地回执流水线

No Vultr Inference key is required. An empty `VULTR_INFERENCE_API_KEY` selects the stub planner. Screenshots land on disk at `screenshots/<sandbox_id>/<index>.png` (gitignored). The same file is served at `GET /api/screenshots/<sandbox_id>/<index>.png`.

### 1. Build and start

```bash
cp .env.example .env          # leave every key empty
docker compose up --build
```

`docker compose up --build` builds `research-with-receipts-sandbox:latest`, runs a one-shot container that prints `sandbox image ready` and exits, then serves the orchestrator on port 8080. Per-request browsers are separate `docker run --rm` containers. The first build pulls the Playwright base image and takes a while.

Job files are written to `var/sandbox-jobs` inside the orchestrator and bind-mounted so the host Docker daemon can see them. Leave `HOST_SANDBOX_JOBS_DIR` empty unless that detection fails.

Host uvicorn (same image, no orchestrator container) also works:

```bash
docker compose build sandbox
uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}"
```

### 2. Endpoints

| Method | Path | Body | What it does |
|--------|------|------|----------------|
| `GET` | `/` | | Health. `inference` is `stub` or `configured` |
| `POST` | `/api/plan` | `{"question":"..."}` | URL plan only. Stub planner when the key is empty |
| `POST` | `/api/sandbox/run` | `{"urls":["https://..."]}` | Playwright sandbox → screenshot receipts |
| `POST` | `/api/research` | `{"question":"..."}` | Plan, then sandbox, then claims. Refuses without receipts |
| `POST` | `/api/claims/validate` | claim object | Schema check (statement, URL, quote, screenshot, timestamps) |
| `GET` | `/api/screenshots/{sandbox_id}/{index}.png` | | The PNG receipt |

`/api/sandbox/run` accepts 1–5 absolute `http`/`https` URLs and rejects `file:`, `javascript:`, and URLs with embedded credentials. A published receipt looks like:

```json
{
  "screenshot_id": "rwr-abc123abc123-0",
  "sandbox_id": "rwr-abc123abc123",
  "index": 0,
  "url": "https://example.com/",
  "relative_path": "screenshots/rwr-abc123abc123/0.png",
  "title": "Example Domain",
  "quote": "Example Domain This domain is for use in illustrative examples...",
  "page_text": "Example Domain This domain is for use in illustrative examples in documents. ...",
  "captured_at": "2026-11-04T03:00:00+00:00",
  "byte_size": 12345,
  "ok": true,
  "error": null
}
```

A claim is returned only when `claim`, `url`, `quote`, `screenshot_path` (under `screenshots/`), `captured_at`, and `created_at` are present, the PNG exists, and the quote passes verification. Returned claims use `verdict: supported` and `verifier: text` or `vision`. `pending_vision_check` is not a valid verdict. `POST /api/claims/validate` checks the schema only and does not look for the file.

### 3. Smoke test without Inference

```bash
curl -sS http://127.0.0.1:8080/
curl -sS http://127.0.0.1:8080/api/plan \
  -H 'content-type: application/json' \
  -d '{"question":"What does example.com say it is for?"}'
curl -sS http://127.0.0.1:8080/api/sandbox/run \
  -H 'content-type: application/json' \
  -d '{"urls":["https://example.com/"]}'
curl -sS http://127.0.0.1:8080/api/research \
  -H 'content-type: application/json' \
  -d '{"question":"What does example.com say it is for?"}'
```

Or `scripts/d2_smoke.sh http://127.0.0.1:8080` after the API is up.

The example.com question is deterministic: the stub plans `https://example.com/` and `https://httpbin.org/html`, not example.com alone. Other questions map by keyword (`python`, `playwright`, `docker`, `vultr`, `wikipedia`). `/api/plan` still has a hash fallback onto that public-page pool; `POST /api/research` does not open those pages or turn them into claims (see Verification and refuse). Confirm `./screenshots/<sandbox_id>/0.png` exists and `GET /api/screenshots/<sandbox_id>/0.png` returns `image/png`. `destroyed` on a researched sourceable question is `true`.

Contract tests (no Docker, no API key):

```bash
python -m unittest discover -s tests -t .
```

---

## Verification and refuse / 校验与拒答

`POST /api/research` never returns an unverified claim. `pending_vision_check` has been removed. Each candidate must pass a quote check, and the PNG must be a real file under `screenshots/`.

### Text check (default, no Inference key)

The sandbox stores the page text beside the PNG (`page_text` on the receipt, `text` in `/job/out/{i}.json`). The claim's `quote` is accepted only when that string appears in `page_text` or in the page title, after whitespace and case are folded. A quote that is not in the capture is dropped. A file that is not a PNG is dropped. Digits in the claim statement (years, version numbers) must also appear in the URL, title, or captured text — a statement cannot introduce a number the page did not show.

`VERIFICATION_MODE=text` forces this path even when an Inference key is present. With the key empty, `auto` is the same path and does not call the network.

### Vision check (optional)

When `VULTR_INFERENCE_API_KEY` and `VULTR_INFERENCE_MODEL` are both set and `VERIFICATION_MODE` is `auto` or `vision`, the orchestrator sends the PNG and the quote to Vultr Serverless Inference and asks for `{"visible": true}` or `{"visible": false}`.

- `visible: false` drops the claim even if the DOM text contains the quote (the words are not on the screenshot).
- `visible: true` keeps the claim (`verifier: vision`).
- A transport error, a missing model, or an unparseable reply falls back to the text check. The failure cannot create a claim.

`GET /` reports the active backend as `verification: text` or `verification: vision`.

### When the API refuses

The response is UI-ready: `status` is `refused`, `claims` is `[]`, and `refusal` is a sentence you can show. `refusal_code` says why.

| `refusal_code` | When | What was not invented |
|----------------|------|------------------------|
| `unsourceable` | The stub planner has no page for this question (the key is empty, or Inference failed and fell back). Fictional and other unmatched questions take this path. | No sandbox, no URLs, no claims. `visits` is `[]`. `/api/plan` may still show the deterministic fallback pool; research does not use it. |
| `unverified` | Pages were opened and a PNG exists, but no quote survived the text or vision check. | Those screenshots stay in `receipts`. They are not turned into claims. |
| `no_receipt` | Docker is missing, the image is missing, or the sandbox wrote no PNG. | No screenshot is fabricated. |

```bash
curl -sS http://127.0.0.1:8080/api/research \
  -H 'content-type: application/json' \
  -d '{"question":"When was the city of Zorblax founded?"}'
```

```json
{
  "status": "refused",
  "claims": [],
  "visits": [],
  "receipts": [],
  "refusal": "No supported source is available for this question. Refusing to invent claims or citations.",
  "refusal_code": "unsourceable",
  "sandbox_id": null,
  "destroyed": false
}
```

A sourceable question such as “What does example.com say it is for?” still plans real URLs. It returns `status: ok` only for claims whose quote matched the capture and whose PNG is on disk. If every quote fails, the same endpoint returns `refusal_code: unverified` and zero claims.

### Secret whitelist

`app/sandbox_runner.py` passes only `SANDBOX_ENV_WHITELIST` into `docker run -e`:

- `HOME=/tmp`
- `XDG_CACHE_HOME=/tmp`
- `XDG_CONFIG_HOME=/tmp`
- `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright`

Values are literals, not copies of the host environment. There is no `--env-file`. The docker client process environment also has `VULTR_*` and other key/token variables removed. The worker drops those names again if they show up inside the container. `scripts/demo_containment.sh` runs this check before the optional Docker inspect.

---

## How Vultr is used / Vultr 用法

1. **VM** — Hosts this FastAPI orchestrator (public demo URL later).
2. **Serverless Inference** — Optional. Research does not call it while `VULTR_INFERENCE_API_KEY` is empty: planning stays on the stub and quote checks stay on the captured page text. When a key and `VULTR_INFERENCE_MODEL` are set, plan requests and (unless `VERIFICATION_MODE=text`) vision quote-checks use the OpenAI-compatible chat completions API. The key stays on the orchestrator. Planning failures fall back to the stub, which still refuses questions it cannot source.
3. **Sandbox compute** — Browser work runs in Docker on the VM (Pattern B); optional throwaway instances can be added later with `VULTR_API_KEY`.

No credentials are invented or committed — env placeholders only.

---

## Project layout

```
research-with-receipts/
  README.md
  .env.example
  .gitignore
  docker-compose.yml
  requirements.txt
  app/                 # FastAPI orchestrator (schemas, planner, verifier, sandbox runner)
  sandbox/worker.py    # Playwright job worker (runs inside the container)
  docker/sandbox/      # Sandbox Dockerfile
  scripts/d2_smoke.sh  # Curl the D2 endpoints
  scripts/demo_containment.sh
  screenshots/         # PNG receipts: screenshots/<sandbox_id>/<index>.png
  var/sandbox-jobs/    # Per-job bind mount (gitignored)
```

Suggested remote: `https://github.com/nomadZyt/research-with-receipts`

---

## License / status

D3 verifies every returned claim: the quote must appear in the captured page (text, or vision when Inference is configured) and the PNG must be on disk. Unsourceable questions and failed checks return `status: refused` with zero claims. The sandbox environment is a fixed whitelist and never receives `VULTR_INFERENCE_API_KEY`.
