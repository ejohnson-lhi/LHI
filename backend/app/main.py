"""FastAPI application entry point.

Run locally with:
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

Or use scripts/run_dev.bat which also starts the Cloudflare Tunnel.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import settings
from app.db.database import init_db
from app.routes import admin, dcs_relay, incoming_call, iris_dashboard, llm, portal, portal_card, sms_signup, vapi_tools
from app.services.iris_dashboard import call_processor as iris_call_processor

# Ensure SQLAlchemy sees all models before init_db's metadata.create_all runs.
# Importing the module is enough — the class registers itself with Base.
import app.models.portal_token  # noqa: F401
import app.models.sms_consent  # noqa: F401
import app.models.pet_declaration  # noqa: F401
import app.models.signature_agreement  # noqa: F401
import app.models.pay_by_link  # noqa: F401

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("lighthouse-backend")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown hooks."""
    log.info(f"Starting Lighthouse backend in {settings.app_env} mode")
    log.info(f"Database: {settings.database_url}")
    await init_db()

    # Iris dashboard call processor: scans recordings dir on an interval,
    # generates Claude summaries for new transcripts, and posts them to
    # the linked Cloudbeds reservation as a note. Gated by
    # IRIS_CALL_PROCESSOR_ENABLED env (default true).
    processor_task: asyncio.Task | None = None
    if iris_call_processor.is_enabled():
        processor_task = asyncio.create_task(
            iris_call_processor.run_loop(iris_call_processor.interval_seconds()),
            name="iris_call_processor",
        )
        log.info(
            "iris_call_processor started (interval=%ds)",
            iris_call_processor.interval_seconds(),
        )
    else:
        log.info("iris_call_processor disabled by IRIS_CALL_PROCESSOR_ENABLED")

    yield

    if processor_task is not None:
        processor_task.cancel()
        try:
            await processor_task
        except (asyncio.CancelledError, Exception):
            pass

    log.info("Shutting down Lighthouse backend")


app = FastAPI(
    title="Lighthouse Inn AI Reservation Agent — Backend",
    version="0.1.0",
    description=(
        "Webhook backend for Iris (the Vapi-based voice agent). "
        "Hosts Cloudbeds tools, call routing, admin commands, and call data archival."
    ),
    lifespan=lifespan,
)

app.include_router(vapi_tools.router, prefix="/tools", tags=["vapi-tools"])
app.include_router(incoming_call.router, prefix="/twilio", tags=["twilio"])
app.include_router(admin.router, prefix="/admin", tags=["admin"])
app.include_router(llm.router, prefix="/llm", tags=["custom-llm"])

# DCS tunnel relay — stable URL on the droplet that 302-redirects into the
# hotel's current ngrok tunnel. Registered before portal so its literal
# /dcs and /portal/dcs-tunnel routes aren't shadowed; the catch-all
# /dcs/{path} is namespaced under /dcs/ so it can't collide with portal's
# /c/, /g/, /h* routes.
app.include_router(dcs_relay.router, tags=["dcs-relay"])

# Iris call-review dashboard — internal tool for the hotel owner to scan
# recent calls, listen to merged audio, read transcripts, and see per-call
# cost and category. Auth via HTTP Basic with portal_shared_secret as the
# password. Mounted under /iris/* — registered alongside dcs_relay so both
# share the same auth posture.
app.include_router(iris_dashboard.router, tags=["iris-dashboard"])

# Guest portal card-capture flow (Stripe.js -> tok_xxx -> Cloudbeds
# internal save_credit_card). Test endpoints under /portal-card/* — no
# auth yet, so localhost / dev use only until we layer a one-shot token
# on top. Registered with the portal-card prefix so the catch-all /h*
# routes in portal.py can't shadow it.
app.include_router(portal_card.router, tags=["portal-card"])

# WordPress /sms-signup/ Fluent Forms webhook. Stable URL on the droplet
# the WP form POSTs to with X-Signup-Secret. This IS the verifiable
# opt-in path cited in our Twilio A2P 10DLC campaign submission. Mount
# before portal so /sms-signup/{anything} can't get swallowed by
# portal's catch-all guest-facing routes.
app.include_router(sms_signup.router, prefix="/sms-signup", tags=["sms-signup"])


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Log raw request body when Pydantic validation fails.

    Without this, third-party callers (Vapi, Twilio, etc.) hitting our endpoints
    with unexpected payload shapes just get a generic 422 and we have to guess
    what they sent. This dumps the body to the log so we can fix the model.
    """
    body = await request.body()
    log.warning(
        "Validation error on %s %s: errors=%s body=%s",
        request.method, request.url.path, exc.errors(), body.decode("utf-8", errors="replace")[:2000],
    )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.get("/health")
async def health():
    """Health check endpoint — used by uptime monitors and dev confirmation."""
    return {
        "status": "ok",
        "env": settings.app_env,
        "service": "lighthouse-backend",
        "version": "0.1.0",
    }


@app.get("/")
async def root():
    """Root endpoint — quick sanity check."""
    return {
        "service": "lighthouse-backend",
        "docs": "/docs",
        "health": "/health",
    }


# Portal router registered LAST so its `/h{stem}` greedy route doesn't
# shadow `/health` and any future literal routes. FastAPI matches in
# registration order; the literal routes above win when the path matches.
# Portal serves /portal/* (DCS-facing) and /c/*, /g/*, /h* (guest-facing)
# under their own access controls.
app.include_router(portal.router, tags=["portal"])
