"""SQLite database setup using SQLAlchemy async.

For v1: SQLite stored in `data/lighthouse.db`. Migrate to PostgreSQL when
deploying to production on DigitalOcean — change DATABASE_URL in .env.
"""
import logging

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
    """Create all tables. Call once at app startup (or use Alembic migrations later)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("Database initialized")
