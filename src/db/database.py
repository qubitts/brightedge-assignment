import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def create_engine(database_url: str):
    """Create an async SQLAlchemy engine."""
    return create_async_engine(database_url, echo=False, pool_pre_ping=True)


def create_session_factory(engine) -> async_sessionmaker[AsyncSession]:
    """Create an async session factory."""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db(engine):
    """Create all tables. Uses create_all for the PoC (no migrations)."""
    from src.db.models import CrawlResult  # noqa: F401 — ensure model is registered

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables created")
