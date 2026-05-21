"""SQLite database setup using SQLAlchemy async.

For v1: SQLite stored in `data/lighthouse.db`. Migrate to PostgreSQL when
deploying to production on DigitalOcean — change DATABASE_URL in .env.
"""
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

from app.config import settings

log = logging.getLogger(__name__)

engine = create_async_engine(settings.database_url, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


async def get_db():
    """FastAPI dependency for getting a database session."""
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    """Create all tables and apply lightweight migrations. Call once at startup
    (replace with Alembic when the schema starts changing more frequently)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # New tables registered via Base.metadata.create_all run above; the
        # explicit ALTER TABLE migrations below handle additive column changes
        # on tables that already exist on older DBs.
        # Idempotent ADD COLUMN migrations. SQLite raises OperationalError on
        # duplicate column; we swallow that. Each entry is (table, col, type).
        migrations = [
            ("portal_token", "twilio_sid", "TEXT"),
            ("sms_consent", "twilio_lookup_at", "DATETIME"),
            ("sms_consent", "twilio_line_type", "TEXT"),
            ("sms_consent", "twilio_carrier", "TEXT"),
        ]
        for table, column, coltype in migrations:
            try:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"))
                log.info("Database: added %s.%s", table, column)
            except Exception:
                pass  # column already exists
    log.info("Database initialized")
