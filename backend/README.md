# Lighthouse Inn AI Reservation Agent — Backend

FastAPI webhook backend for **Iris**, the Vapi-based voice agent for The Lighthouse Inn.

## What this serves

- **Vapi tool endpoints** (`/tools/*`) — Iris calls these mid-conversation for Cloudbeds lookups, reservation creation, payment, SMS, etc.
- **Twilio inbound webhook** (`/twilio/incoming-call`) — pre-call routing (block list, forward-mode, AI handoff)
- **Admin endpoints** (`/admin/*`) — backend monitoring and configuration
- **Health check** (`/health`) — for uptime monitors

## v1 status

This is the **skeleton** — endpoints exist and return stub responses. Cloudbeds, Twilio, Stripe, and Vapi integrations are stubbed in `app/tools/` and need to be wired up before production use.

## Setup (one-click)

Double-click `setup.bat`. It creates the virtual environment, installs dependencies, copies `.env.example` to `.env`, and tells you what to do next.

You also need to install **Cloudflare Tunnel** separately (for exposing the local backend to Vapi/Twilio over HTTPS):
- https://github.com/cloudflare/cloudflared/releases — download the Windows installer (`cloudflared-windows-amd64.msi`) and add `cloudflared` to your PATH.

## Setup (manual, cross-platform)

```cmd
REM From this folder (D:\...\AI Reservation Agent\backend\):
python -m venv .venv
.venv\Scripts\activate
pip install -e .
copy .env.example .env
REM Edit .env with real values (Twilio, Cloudbeds, Anthropic, Stripe, Vapi keys)
```

## Run locally

Double-click `scripts\run_dev.bat`, or:
```cmd
.venv\Scripts\activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**Port note**: the backend runs on **port 8000**. Port 8080 is used by the GX-26 hotel system (door codes, Z-Wave, Cloudbeds room assignments, housekeeping management) which runs on the same machine — don't change to 8080.

Opens two windows:
1. FastAPI on `http://localhost:8000` (with `/docs` for the Swagger UI)
2. Cloudflare Tunnel — gives you a public HTTPS URL to paste into Vapi/Twilio webhook config

## Project structure

```
backend\
├── pyproject.toml              ← project metadata + dependencies
├── .env.example                ← config template (copy to .env)
├── README.md                   ← this file
├── app\
│   ├── main.py                 ← FastAPI app entry, /health, /
│   ├── config.py               ← env-loaded settings
│   ├── routes\
│   │   ├── vapi_tools.py       ← /tools/* — Vapi tool endpoints
│   │   ├── incoming_call.py    ← /twilio/incoming-call — inbound routing
│   │   └── admin.py            ← /admin/* — monitoring
│   ├── tools\
│   │   ├── cloudbeds.py        ← Cloudbeds API wrapper (stubbed)
│   │   ├── twilio_sms.py       ← SMS sending wrapper (stubbed)
│   │   └── stripe_pay.py       ← Stripe wrapper (stubbed)
│   ├── models\
│   │   ├── vapi_payloads.py    ← Pydantic models for Vapi webhooks
│   │   └── routing_state.py    ← call routing state model
│   └── db\
│       └── database.py         ← SQLAlchemy async engine + session
├── scripts\
│   └── run_dev.bat             ← local dev runner (FastAPI + tunnel)
└── data\                       ← SQLite database lives here (gitignored)
```

## Next steps after skeleton

1. Wire up `lookup_reservation_by_phone` to a real Cloudbeds API call (highest value — unlocks caller-ID auth + lockout self-service)
2. Wire up `create_reservation` (the booking-flow primary tool)
3. Wire up Twilio SMS in `twilio_sms.py` (replaces the `[STUB SMS]` log lines)
4. Implement the call routing state DB model (currently inline literal in `admin.py`)
5. Implement the block list DB model
6. Build the build/sync script that uploads `Lighthouse_AI_system_prompt-2026may02.txt` + `knowledge_base.md` to Vapi

See `D:\2-Work\ComputerSoftwareDevelopment\AI Reservation Agent\design.md` for the full TODO list and architectural decisions.

## Deployment (later)

Production target: DigitalOcean SFO3 ($6/month Basic Droplet). See design.md for the deployment plan. Switch `DATABASE_URL` to PostgreSQL when deploying.
