"""FastAPI application entry point.

Run locally with:
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

Or use scripts/run_dev.bat which also starts the Cloudflare Tunnel.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import settings
from app.db.database import init_db
from app.routes import admin, incoming_call, llm, portal, vapi_tools

# Ensure SQLAlchemy sees all models before init_db's metadata.create_all runs.
# Importing the module is enough — the class registers itself with Base.
import app.models.portal_token  # noqa: F401
import app.models.sms_consent  # noqa: F401
import app.models.pet_declaration  # noqa: F401
import app.models.signature_agreement  # noqa: F401

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
    yield
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
