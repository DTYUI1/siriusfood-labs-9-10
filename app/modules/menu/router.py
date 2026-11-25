"""
Транспортный слой модуля menu (круг 4).
Читать меню может любой клиент, изменять — только повар и администратор.
"""
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam
from redis import Redis
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.core.dependencies import require_role
from app.core.identity import CurrentUser, RoleName
from app.core.redis import get_cache
from app.database import get_db
from app.exceptions import DomainError
from app.http_errors import to_http_exception
from app.modules.menu.repository import MenuRepository
from app.modules.menu.service import MenuItem, MenuService
from app.schemas import CategoryCreate, MenuItemCreate, MenuItemUpdate

router = APIRouter()

MIN_NAME_LENGTH = 2

# Студент и курьер получают 403.
menu_editor = require_role(RoleName.COOK, RoleName.ADMIN)


def get_menu_service(db: Session = Depends(get_db), cache: Redis | None = Depends(get_cache)) -> MenuService:
    """Сборка архитектурных слоёв модуля menu. Кеш передаётся в репозиторий, а не в сервис."""
    return MenuService(MenuRepository(db, cache))


@router.get("/items")
def get_menu(service: MenuService = Depends(get_menu_service)):
    """Всё меню. Публичный эндпоинт: в меню нет персональных данных."""
    return [asdict(item) for item in service.get_menu()]


@router.get("/items/{item_id}", responses={404: {"description": "Блюдо не найдено"}})
def get_menu_item(
    item_id: Annotated[int, PathParam(gt=0, le=2_147_483_647)],
    service: MenuService = Depends(get_menu_service),
) -> MenuItem:
    """Публичная карточка блюда по идентификатору."""
    try:
        return service.get_item(item_id)
    except DomainError as error:
        raise to_http_exception(error) from error


@router.get("/categories")
def get_categories(service: MenuService = Depends(get_menu_service)):
    return [asdict(category) for category in service.get_categories()]


@router.get("/categories/{category_id}")
def get_category_menu(category_id: int, service: MenuService = Depends(get_menu_service)):
    """Категория и её блюда."""
    try:
        category_menu = service.get_category_menu(category_id)
    except DomainError as error:
        raise to_http_exception(error) from error

    return {
        **asdict(category_menu.category),
        "items": [asdict(item) for item in category_menu.items],
    }


@router.post("/categories")
def create_category(
    payload: CategoryCreate,
    _editor: CurrentUser = Depends(menu_editor),
    service: MenuService = Depends(get_menu_service),
):
    name = payload.name.strip()
    if not MIN_NAME_LENGTH <= len(name) <= 100:
        raise HTTPException(status_code=400, detail="Название категории слишком короткое")

    try:
        category_id = service.create_category(name)
    except DomainError as error:
        raise to_http_exception(error) from error

    return {"status": "success", "category_id": category_id}


@router.patch("/categories/{category_id}")
def update_category(category_id: int, payload: CategoryCreate,
                    _editor: CurrentUser = Depends(menu_editor), service: MenuService = Depends(get_menu_service)):
    name = payload.name.strip()
    if not MIN_NAME_LENGTH <= len(name) <= 100:
        raise HTTPException(422, detail={"code": "invalid_category", "message": "Название: от 2 до 100 символов"})
    try:
        service.update_category(category_id, name)
    except IntegrityError as error:
        raise HTTPException(409, detail={"code": "duplicate_category", "message": "Название уже используется"}) from error
    except DomainError as error:
        raise to_http_exception(error) from error
    return {"status": "success", "category_id": category_id}


@router.post("/items")
def create_menu_item(
    payload: MenuItemCreate,
    _editor: CurrentUser = Depends(menu_editor),
    service: MenuService = Depends(get_menu_service),
):
    # Техническая валидация цены и названия.
    if payload.price <= 0:
        raise HTTPException(status_code=400, detail="Цена должна быть положительной")

    if len(payload.name) < MIN_NAME_LENGTH:
        raise HTTPException(status_code=400, detail="Название блюда слишком короткое")

    try:
        item_id = service.create_item(name=payload.name, price=payload.price, category_id=payload.category_id, description=payload.description)
    except DomainError as error:
        raise to_http_exception(error) from error

    return {"status": "success", "item_id": item_id}


@router.patch("/items/{item_id}")
def update_menu_item(
    item_id: int,
    payload: MenuItemUpdate,
    _editor: CurrentUser = Depends(menu_editor),
    service: MenuService = Depends(get_menu_service),
):
    # Техническая валидация нового значения цены.
    if payload.price is not None and payload.price <= 0:
        raise HTTPException(status_code=400, detail="Цена должна быть положительной")

    try:
        service.update_item(
            item_id,
            name=payload.name,
            price=payload.price,
            is_available=payload.is_available,
            category_id=payload.category_id,
            description=payload.description,
            description_set="description" in payload.model_fields_set,
        )
    except DomainError as error:
        raise to_http_exception(error) from error

    return {"status": "success", "item_id": item_id}
