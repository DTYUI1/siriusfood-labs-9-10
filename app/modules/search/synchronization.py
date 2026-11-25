"""Bounded periodic projection, independent from the financial outbox loop."""
import asyncio
import logging

from app.core.product_config import product_settings
from app.database import SessionLocal
from app.modules.search.elasticsearch import ElasticsearchMenu
from app.modules.search.repository import PublicMenuSource

logger = logging.getLogger(__name__)


def synchronize_once(session_factory=SessionLocal, config=None):
    config = config or product_settings()
    if not config.es_url:
        return False
    with session_factory() as db:
        items = PublicMenuSource(db).items()
    ElasticsearchMenu(config.es_url, config.es_index, config.state_dir).refresh(items, nonblocking=True)
    return True


async def periodic_menu_projection(*, interval=30, max_backoff=120):
    delay = interval
    while True:
        try:
            enabled = await asyncio.to_thread(synchronize_once)
            if enabled:
                logger.info("[MenuProjection] synchronized")
            delay = interval
        except Exception as error:
            # Only safe class names; credentials/client responses never enter logs.
            logger.warning("[MenuProjection] unavailable (%s)", type(error).__name__)
            delay = min(max_backoff, max(interval, delay * 2))
        await asyncio.sleep(delay)
