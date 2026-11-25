"""
Слой бизнес-логики модуля menu (круги 1–2).
Импорты fastapi, sqlalchemy и redis в этом файле запрещены.
"""
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.exceptions import BusinessRuleError, ConflictError, NotFoundError

if TYPE_CHECKING:
    from app.modules.menu.repository import MenuRepository


@dataclass(frozen=True)
class MenuItem:
    """Блюдо меню (круг 1). Не связано ни с ORM, ни с HTTP."""

    id: int
    name: str
    price: float
    is_available: bool
    category_id: int | None = None
    category_name: str | None = None
    description: str = ""


@dataclass(frozen=True)
class Category:
    id: int
    name: str


@dataclass(frozen=True)
class CategoryMenu:
    """Категория вместе с её блюдами."""

    category: Category
    items: tuple[MenuItem, ...]


class MenuService:
    """Сценарии чтения и изменения меню столовой."""

    def __init__(self, repository: "MenuRepository"):
        self.repository = repository

    def get_menu(self) -> list[MenuItem]:
        """Всё меню, отсортированное по категориям и названиям."""
        return self.repository.get_all()

    def get_item(self, item_id: int) -> MenuItem:
        item = self.repository.get_by_id(item_id)
        if item is None:
            raise NotFoundError("Блюдо не найдено")
        return item

    def get_categories(self) -> list[Category]:
        return self.repository.get_categories()

    def get_category_menu(self, category_id: int) -> CategoryMenu:
        """Категория с блюдами. Возбуждает NotFoundError."""
        category_menu = self.repository.get_category_menu(category_id)
        if category_menu is None:
            raise NotFoundError("Категория не найдена")
        return category_menu

    def create_category(self, name: str) -> int:
        """Добавление категории. Возбуждает BusinessRuleError, если название занято."""
        if self.repository.get_category_by_name(name):
            raise BusinessRuleError("Категория с таким названием уже существует")
        return self.repository.add_category(name)

    def update_category(self, category_id: int, name: str) -> int:
        if not self.repository.get_category(category_id):
            raise NotFoundError("Категория не найдена")
        existing = self.repository.get_category_by_name(name)
        if existing and existing.id != category_id:
            raise ConflictError("Категория с таким названием уже существует", "duplicate_category")
        self.repository.update_category(category_id, name)
        return category_id

    def create_item(self, name: str, price: float, category_id: int | None = None, description: str | None = None) -> int:
        """
        Добавление блюда. Возвращает его идентификатор.
        Возбуждает BusinessRuleError, если блюдо с таким названием уже есть,
        и NotFoundError, если категория не существует.
        """
        if self.repository.get_by_name(name):
            raise BusinessRuleError("Блюдо с таким названием уже существует")
        self._ensure_category_exists(category_id)
        return self.repository.add(name=name, price=price, category_id=category_id, description=description)

    def update_item(
        self,
        item_id: int,
        name: str | None = None,
        price: float | None = None,
        is_available: bool | None = None,
        category_id: int | None = None,
        description: str | None = None,
        description_set: bool = False,
    ) -> int:
        """Изменение блюда. None — поле не меняется. Возбуждает NotFoundError."""
        if not self.repository.get_by_id(item_id):
            raise NotFoundError("Блюдо не найдено")
        self._ensure_category_exists(category_id)

        changes = {
            field: value
            for field, value in {
                "name": name,
                "price": price,
                "is_available": is_available,
                "category_id": category_id,
            }.items()
            if value is not None
        }
        if description_set:
            changes["description"] = description
        if changes:
            self.repository.update(item_id, changes)
        return item_id

    def _ensure_category_exists(self, category_id: int | None) -> None:
        if category_id is not None and not self.repository.get_category(category_id):
            raise NotFoundError("Категория не найдена")
