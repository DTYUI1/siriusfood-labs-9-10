"""
Определение ORM-моделей предметной области «Сириус.Еда».
"""
from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, Float, Boolean, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from app.database import Base


class Role(Base):
    """Роль пользователя: student, cook, courier, admin."""

    __tablename__ = "roles"

    id = Column(Integer, primary_key=True)
    name = Column(String(50), unique=True, nullable=False)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False)
    phone = Column(String, nullable=True)
    balance = Column(Float, default=0.0)
    is_blocked = Column(Boolean, default=False)
    # bcrypt-хеш пароля. Сам пароль нигде не хранится.
    password_hash = Column(String, nullable=False)
    auth_version = Column(Integer, nullable=False, default=0, server_default="0")
    role_id = Column(Integer, ForeignKey("roles.id"), nullable=False)

    role = relationship("Role")


class RefreshToken(Base):
    """Выданный refresh-токен. Нужен для отзыва и ротации."""

    __tablename__ = "refresh_tokens"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # Храним SHA-256 токена, а не сам токен: утечка БД не даёт готовых токенов.
    token_hash = Column(String, unique=True, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    is_revoked = Column(Boolean, nullable=False, default=False)


class Category(Base):
    """Категория меню: супы, горячее, напитки."""

    __tablename__ = "categories"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, nullable=False)


class MenuItem(Base):
    __tablename__ = "menu_items"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    description = Column(String(2000), nullable=True)
    price = Column(Float, nullable=False)
    is_available = Column(Boolean, default=True)
    # nullable: блюда, созданные до появления категорий, остаются без категории.
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=True, index=True)

    category = relationship("Category")


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    item_id = Column(Integer, ForeignKey("menu_items.id"))
    quantity = Column(Integer, nullable=False)
    status = Column(String, default="processing")
    # Списанная сумма. Фиксируется при оформлении: при отмене возвращается
    # именно она, даже если цена блюда потом изменилась.
    total_cost = Column(Float, nullable=False)
    # Immutable recipient snapshot. All NULL identifies a historical/legacy order.
    recipient_name = Column(String(100), nullable=True)
    recipient_phone = Column(String(32), nullable=True)
    recipient_location = Column(String(300), nullable=True)
    # Время оформления в UTC. NULL — заказы, созданные до миграции 0004: их время неизвестно.
    created_at = Column(
        DateTime,
        nullable=True,
        index=True,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    )

    user = relationship("User")
    item = relationship("MenuItem")
    kitchen = relationship("KitchenOrder", uselist=False)


class Delivery(Base):
    __tablename__ = "deliveries"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), unique=True)
    courier_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    status = Column(String, default="pending")

    order = relationship("Order")
    courier = relationship("User")



class KitchenOrder(Base):
    """Запись кухни появляется только после события OrderPaid."""
    __tablename__ = "kitchen_orders"

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), unique=True, nullable=False)
    status = Column(String(30), nullable=False, default="pending")
