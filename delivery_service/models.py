"""
ORM-модели микросервиса. Свои классы, а не app.models: сервис не зависит от кода монолита.

deliveries — таблица сервиса, он единственный, кто в неё пишет.
orders, users, roles — таблицы монолита: сервис читает их, а в orders ещё и
меняет статус при завершении доставки. Это и есть связность через общую БД,
которую убирает следующий шаг: своя БД и события вместо общих таблиц.
"""
from sqlalchemy import Boolean, Column, Float, ForeignKey, Integer, String
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Role(Base):
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True)
    name = Column(String(50), nullable=False)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    is_blocked = Column(Boolean)
    role_id = Column(Integer, ForeignKey("roles.id"))

    role = relationship("Role")


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    status = Column(String)
    total_cost = Column(Float)
    recipient_name = Column(String(100), nullable=True)
    recipient_phone = Column(String(32), nullable=True)
    recipient_location = Column(String(300), nullable=True)


class KitchenOrder(Base):
    __tablename__ = "kitchen_orders"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), unique=True, nullable=False)
    status = Column(String(30), nullable=False)


class Delivery(Base):
    __tablename__ = "deliveries"

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), unique=True)
    courier_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    status = Column(String, default="pending")

    order = relationship("Order")
