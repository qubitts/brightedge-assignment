import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.config import settings
from src.api.routes import router
from src.crawler.fetcher import PageFetcher
from src.cache.redis_cache import CrawlCache

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    logger.info("Starting up...")
    app.state.startup_time = time.time()

    # Init fetcher
    app.state.fetcher = PageFetcher()

    # Init cache
    app.state.cache = CrawlCache()
    await app.state.cache.connect()

    # Init database (graceful degradation — app works without DB)
    db_engine = None
    app.state.db = None
    try:
        from src.db.database import create_engine, create_session_factory, init_db
        from src.db.repository import CrawlRepository

        db_engine = create_engine(settings.DATABASE_URL)
        await init_db(db_engine)
        session_factory = create_session_factory(db_engine)
        app.state.db = CrawlRepository(session_factory)
        logger.info("Database initialized")
    except Exception as e:
        logger.warning(f"Failed to initialize database: {e}")

    # NLP models (KeyBERT, spaCy) are lazy-loaded on first request
    # to avoid blocking startup and exceeding memory limits on free-tier hosts
    logger.info("Startup complete (NLP models will load on first request)")
    yield

    # Shutdown
    logger.info("Shutting down...")
    await app.state.fetcher.close()
    await app.state.cache.close()
    if db_engine:
        await db_engine.dispose()
        logger.info("Database engine disposed")
    logger.info("Shutdown complete")


app = FastAPI(
    title="BrightEdge Crawler Service",
    description="URL Crawler & Content Classification Service",
    version="1.0.0",
    lifespan=lifespan,
    openapi_url=None,
)

app.include_router(router)
