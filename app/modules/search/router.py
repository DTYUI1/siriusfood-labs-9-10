import math
import uuid
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.database import get_db
from app.core.product_config import product_settings
from app.modules.search.elasticsearch import ElasticsearchMenu
from app.modules.search.repository import PublicMenuSource
from app.modules.search.service import SearchService, SearchUnavailable

router = APIRouter()


def get_search_service(db: Session = Depends(get_db)):
    cfg = product_settings()
    return SearchService(PublicMenuSource(db), ElasticsearchMenu(cfg.es_url, cfg.es_index, cfg.state_dir))


def unavailable():
    return HTTPException(503, {"code": "search_unavailable", "request_id": uuid.uuid4().hex})


@router.get('/search')
def search(q: str = Query('', max_length=200), category_id: int | None = Query(None, ge=1),
           min_price: float | None = Query(None, ge=0), max_price: float | None = Query(None, ge=0),
           offset: int = Query(0, ge=0, le=9900), limit: int = Query(50, ge=1, le=100),
           db: Session = Depends(get_db)):
    if any(v is not None and not math.isfinite(v) for v in (min_price, max_price)) or (min_price is not None and max_price is not None and min_price > max_price):
        raise HTTPException(422, "Недопустимый диапазон цены")
    try:
        return get_search_service(db).search(q.strip(), category_id, min_price, max_price, offset, limit)
    except (SearchUnavailable, OSError):
        raise unavailable() from None


@router.get('/suggestions')
def suggestions(q: str = Query('', max_length=200), limit: int = Query(5, ge=1, le=10), db: Session = Depends(get_db)):
    try:
        return get_search_service(db).suggestions(q.strip(), limit)
    except (SearchUnavailable, OSError):
        raise unavailable() from None
