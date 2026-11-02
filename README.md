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
    ↓ if none → refuse
Response {status, claims[], refusal, sandbox_id, destroyed}
```

Flow:

1. **Orchestrator** (on Vultr VM) receives `{question}`, calls **Vultr Serverless Inference** to plan URLs / claims.
2. **Sandbox** — Playwright Chromium in a short-lived Docker container visits URLs, writes full-page screenshots + text excerpts under `/job/out`.
3. Orchestrator **assembles claims** only when screenshot + quote exist; otherwise **refusal**.
4. Container is **destroyed** after the job (`docker run --rm`); secrets never enter the sandbox env.

Containment focus / 遏制重点:

| Requirement | Practice |
|-------------|----------|
| Process isolation | Playwright only inside container; app receives screenshots/text only |
| Secret hygiene | `VULTR_INFERENCE_API_KEY` stays on the VM orchestrator — never passed into sandbox env |
| Resource limits | Per-job time / memory / CPU caps |
| Lifecycle | Destroy after task; log `sandbox_id` |

---

## Stack / 技术栈

- **Vultr VM** — FastAPI orchestrator (`app/`)
- **Vultr Serverless Inference** — `https://api.vultrinference.com/v1` (planning / vision verification later)
- **Docker + Playwright** — Pattern B sandboxed browser (`docker/sandbox`, `sandbox/worker.py`)
- **Python** — FastAPI, uvicorn, pydantic, httpx, python-dotenv

---

## Demo script / Demo 脚本

Record these three beats (containment moment is required for judging):

1. **Happy path** — Ask a factual question (e.g. “When did product X ship?”). Brief returns; each claim opens URL + screenshot + quote.
2. **Refuse** — Ask about a fictional event. Sandbox finds no reliable sources → UI/API returns explicit `refusal`, no invented claims.
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
| `VULTR_INFERENCE_API_KEY` | Yes (for real inference) | Orchestrator only; never inject into sandbox |
| `VULTR_INFERENCE_BASE_URL` | Default | `https://api.vultrinference.com/v1` |
| `VULTR_API_KEY` | Optional | Cloud/API management if you automate VM/sandbox infra |
| `SANDBOX_TIMEOUT_SEC` | Default `120` | Hard timeout budget for sandbox jobs |
| `SANDBOX_MEMORY_MB` | Default `1024` | `docker run --memory` |
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

Health: `GET /`  
Research stub: `POST /api/research` with `{"question":"..."}`.

### Build sandbox image

```bash
docker compose build sandbox
# or:
docker build -t research-with-receipts-sandbox -f docker/sandbox/Dockerfile .
```

The orchestrator spawns `docker run --rm ...` (see `app/sandbox_runner.py`); compose only builds/documents the sandbox image.

---

## How Vultr is used / Vultr 用法

1. **VM** — Hosts this FastAPI orchestrator (public demo URL later).
2. **Serverless Inference** — All LLM calls (plan, later vision quote-check) go through Vultr Inference OpenAI-compatible API; key lives only on the VM.
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
  app/                 # FastAPI orchestrator
  sandbox/worker.py    # Playwright job worker
  docker/sandbox/      # Sandbox Dockerfile
  scripts/demo_containment.sh
  screenshots/         # Host-side artifacts (.gitkeep; PNGs gitignored)
```

Suggested remote: `https://github.com/nomadZyt/research-with-receipts`

---

## License / status

Greenfield scaffold for Vultr Agent Rush — stubs for plan → sandbox → claims; wire real Inference + Playwright end-to-end on D2–D3.
