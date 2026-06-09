# Syzygy Launchpad — AI Service

FastAPI service that powers the AI Assistant panel in the Syzygy launchpad.

- **Model**: SAP AI Core (Generative AI Hub) — `gpt-4o-mini` deployment via the `aicore-genai` destination.
- **Data**: SAP Incentive Management OData APIs via the `TCMP_DEST` destination.
- **Pattern**: Multi-agent. v1 has a super-agent (router) and one sub-agent: **Payment-to-Transaction Traceability**.

## Architecture

```
Browser → Approuter (XSUAA) → ai-service (this) → AI Core (chat)
                                              → TCMP destination (data)
```

Tools are registered in-process (no MCP transport yet — same shape, so it's a thin adapter later).

## Layout

```
ai-service/
  app/
    main.py                 FastAPI entrypoint, /chat, /health
    auth.py                 JWT principal extraction (validation deferred)
    config.py               Env-var settings
    destination_client.py   BTP Destination service client (token cache)
    aicore_client.py        AI Core chat-completions wrapper
    orchestrator.py         Super-agent router
    agents/
      base.py               Agent dataclass + run loop
      payment_traceability.py
    tools/
      payments_api.py       query_payments tool (CS_PAYMENT via TCMP)
    schemas.py              Pydantic request/response models
  requirements.txt
  runtime.txt               python-3.12.6 for CF buildpack
  Procfile                  uvicorn launcher
```

## Endpoints

| Method | Path     | Notes                                               |
|--------|----------|-----------------------------------------------------|
| GET    | `/health`| Liveness                                            |
| POST   | `/chat`  | `{messages:[{role,content}]}` → `{reply, agent, tool_calls}` |

## Local development

```bash
cd ai-service
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env:
#   - set AICORE_SERVICE_KEY to the raw JSON of your AI Core service key
#   - set DEV_SKIP_AUTH=true
uvicorn app.main:app --reload --port 8080
```

Then:

```bash
curl -s http://localhost:8080/health
curl -s -X POST http://localhost:8080/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Show payments in period 2533274790396033"}]}' | python3 -m json.tool
```

> Local mode only supports `aicore-genai`. The TCMP destination requires the BTP Destination service binding (i.e., run in CF).

## Cloud Foundry

The service is deployed as part of the launchpad MTA (`syzygy-launchpad-ai` module). The MTA binding gives it:
- the destination service (so it can resolve `aicore-genai` and `TCMP_DEST`)
- the XSUAA instance (for JWT validation in a future iteration)

Env vars are set as `properties:` on the module in mta.yaml.

## Adding more agents

1. Create `app/agents/<your_agent>.py` exporting `build_agent() -> Agent`.
2. Register it in `app/orchestrator.py` (`self.register(build_<your>_agent())`).
3. Add any tools under `app/tools/` and expose a JSON-schema tool spec.

The router will then LLM-pick between agents automatically.
