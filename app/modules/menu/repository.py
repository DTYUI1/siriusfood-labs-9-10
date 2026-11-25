"""
Инфраструктурный слой модуля menu (круг 3: Interface Adapters).

Чтение меню кешируется в Redis по стратегии Cache-Aside. Кеш — деталь
хранения, поэтому он здесь, а не в service.py: сервис вызывает get_all()
и не знает, ответил PostgreSQL или Redis.
"""
from dataclasses import asdict

from redis import Redis
from sqlalchemy.orm import Session

from app.core.cache import CacheAside
from app.core.config import settings
from app.models import Category as CategoryModel
from app.models import MenuItem as MenuItemModel
from app.modules.menu.service import Category, CategoryMenu, MenuItem

MENU_KEY = "menu:all"
CATEGORIES_KEY = "menu:categories"


def category_key(category_id: int) -> str:
    return f"menu:category:{category_id}"


def _dump_items(items) -> list[dict]:
    return [asdict(item) for item in items]


def _restore_items(data: list[dict]) -> list[MenuItem]:
    return [MenuItem(**item) for item in data]


def _dump_category_menu(category_menu: CategoryMenu) -> dict:
    return {"category": asdict(category_menu.category), "items": _dump_items(category_menu.items)}


def _restore_category_menu(data: dict | None) -> CategoryMenu | None:
    if data is None:
        return None
    return CategoryMenu(category=Category(**data["category"]), items=tuple(_restore_items(data["items"])))


class MenuRepository:
    """Операции с таблицами menu_items и categories. Бизнес-правила здесь отсутствуют."""

    def __init__(self, db: Session, cache: Redis | None = None):
        self.db = db
        self.cache = CacheAside(cache)

    def get_all(self) -> list[MenuItem]:
        """Всё меню: JOIN с категориями, сортировка по категории и названию. Кешируется."""
        return self.cache.get_or_load(
            MENU_KEY,
            ttl=settings.MENU_CACHE_TTL,
            load=self._load_items,
            dump=_dump_items,
            restore=_restore_items,
        )

    def get_categories(self) -> list[Category]:
        """Список категорий. Кешируется на 10 минут."""
        return self.cache.get_or_load(
            CATEGORIES_KEY,
            ttl=settings.CATEGORY_CACHE_TTL,
            load=self._load_categories,
            dump=lambda categories: [asdict(category) for category in categories],
            restore=lambda data: [Category(**category) for category in data],
        )

    def get_category_menu(self, category_id: int) -> CategoryMenu | None:
        """
        Категория с блюдами, ключ menu:category:{id}, TTL 10 минут.
        Отсутствие категории тоже кешируется (на минуту): иначе запросы
        к несуществующим id всегда доходили бы до БД (cache penetration).
        """
        return self.cache.get_or_load(
            category_key(category_id),
            ttl=settings.CATEGORY_CACHE_TTL,
            load=lambda: self._load_category_menu(category_id),
            dump=_dump_category_menu,
            restore=_restore_category_menu,
            empty_ttl=settings.NOT_FOUND_CACHE_TTL,
        )

    def warm_up(self) -> int:
        """
        Прогрев кеша при старте: меню, список категорий и каждая категория
        записываются в Redis заранее, и первый запрос не идёт в БД.
        Возвращает число блюд.
        """
        items = self._load_items()
        categories = self._load_categories()
        self.cache.put(MENU_KEY, settings.MENU_CACHE_TTL, _dump_items(items))
        self.cache.put(CATEGORIES_KEY, settings.CATEGORY_CACHE_TTL, [asdict(category) for category in categories])
        for category in categories:
            category_items = tuple(item for item in items if item.category_id == category.id)
            self.cache.put(
                category_key(category.id),
                settings.CATEGORY_CACHE_TTL,
                _dump_category_menu(CategoryMenu(category=category, items=category_items)),
            )
        return len(items)

    def get_category(self, category_id: int) -> Category | None:
        row = self.db.query(CategoryModel).filter(CategoryModel.id == category_id).first()
        return Category(id=row.id, name=row.name) if row else None

    def get_category_by_name(self, name: str) -> Category | None:
        row = self.db.query(CategoryModel).filter(CategoryModel.name == name).first()
        return Category(id=row.id, name=row.name) if row else None

    def get_by_id(self, item_id: int) -> MenuItem | None:
        result = (
            self.db.query(MenuItemModel, CategoryModel.name)
            .outerjoin(CategoryModel, MenuItemModel.category_id == CategoryModel.id)
            .filter(MenuItemModel.id == item_id)
            .first()
        )
        return self._to_entity(*result) if result is not None else None

    def get_by_name(self, name: str) -> MenuItem | None:
        row = self.db.query(MenuItemModel).filter(MenuItemModel.name == name).first()
        return self._to_entity(row) if row else None

    def add(self, name: str, price: float, category_id: int | None = None, description: str | None = None) -> int:
        """Сохранение нового блюда. Возвращает его идентификатор."""
        item = MenuItemModel(name=name, price=price, is_available=True, category_id=category_id, description=description)
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        # Инвалидация после commit: иначе параллельный запрос успел бы
        # положить в кеш старое меню, прочитанное до фиксации транзакции.
        self.cache.invalidate(*self._item_keys(category_id))
        return item.id

    def update(self, item_id: int, changes: dict) -> None:
        """Запись изменённых полей блюда."""
        # Блюдо могли перенести в другую категорию: сбросить нужно обе.
        old_category_id = (
            self.db.query(MenuItemModel.category_id).filter(MenuItemModel.id == item_id).scalar()
        )
        self.db.query(MenuItemModel).filter(MenuItemModel.id == item_id).update(changes)
        self.db.commit()
        self.cache.invalidate(*self._item_keys(old_category_id, changes.get("category_id")))

    def add_category(self, name: str) -> int:
        category = CategoryModel(name=name)
        self.db.add(category)
        self.db.commit()
        self.db.refresh(category)
        # Сбрасывается и закешированное «категории нет» для нового id.
        self.cache.invalidate(CATEGORIES_KEY, category_key(category.id))
        return category.id

    def update_category(self, category_id: int, name: str) -> None:
        self.db.query(CategoryModel).filter_by(id=category_id).update({CategoryModel.name: name})
        self.db.commit()
        self.cache.invalidate(MENU_KEY, CATEGORIES_KEY, category_key(category_id))

    @staticmethod
    def _item_keys(*category_ids: int | None) -> list[str]:
        """Ключи, которые устаревают при изменении блюда."""
        return [MENU_KEY, *(category_key(category_id) for category_id in set(category_ids) if category_id)]

    def _load_categories(self) -> list[Category]:
        rows = self.db.query(CategoryModel).order_by(CategoryModel.name).all()
        return [Category(id=row.id, name=row.name) for row in rows]

    def _load_category_menu(self, category_id: int) -> CategoryMenu | None:
        category = self.get_category(category_id)
        if category is None:
            return None
        return CategoryMenu(category=category, items=tuple(self._load_items(category_id)))

    def _load_items(self, category_id: int | None = None) -> list[MenuItem]:
        query = (
            self.db.query(MenuItemModel, CategoryModel.name)
            .outerjoin(CategoryModel, MenuItemModel.category_id == CategoryModel.id)
            .order_by(CategoryModel.name.asc().nulls_last(), MenuItemModel.name)
        )
        if category_id is not None:
            query = query.filter(MenuItemModel.category_id == category_id)
        return [self._to_entity(row, category_name) for row, category_name in query.all()]

    @staticmethod
    def _to_entity(row: MenuItemModel, category_name: str | None = None) -> MenuItem:
        """Преобразование ORM-модели в доменную сущность."""
        return MenuItem(
            id=row.id,
            name=row.name,
            price=row.price,
            is_available=row.is_available,
            category_id=row.category_id,
            category_name=category_name,
            description=row.description or "",
        )
