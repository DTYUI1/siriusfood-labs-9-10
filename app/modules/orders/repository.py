"""
Инфраструктурный слой модуля orders (круг 3: Interface Adapters).
"""
import logging
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import KitchenOrder, MenuItem, Order, User
from app.exceptions import ConflictError, NotFoundError
from event_bus.observability import event_log_fields
from event_bus.storage import append_event

logger = logging.getLogger(__name__)


class OrderRepository:
    """
    Класс отвечает исключительно за выполнение операций ввода-вывода
    в контексте СУБД PostgreSQL с использованием SQLAlchemy ORM.
    Бизнес-правила в данном классе отсутствуют.
    """

    def __init__(self, db: Session):
        self.db = db

    def get_user_by_id(self, user_id: int) -> User | None:
        """Извлечение объекта пользователя по идентификатору."""
        return self.db.query(User).filter(User.id == user_id).first()

    def get_menu_item_by_id(self, item_id: int) -> MenuItem | None:
        """Извлечение объекта блюда из меню по идентификатору."""
        return self.db.query(MenuItem).filter(MenuItem.id == item_id).first()

    def get_order(self, order_id: int) -> Order | None:
        """Извлечение заказа по идентификатору."""
        return self.db.query(Order).filter(Order.id == order_id).first()

    def get_orders_by_status(self, status: str) -> list[Order]:
        """Заказы в указанном статусе, от старых к новым."""
        return self.db.query(Order).filter(Order.status == status).order_by(Order.id).all()

    def own_orders(self, user_id: int) -> list[Order]:
        return self.db.query(Order).filter(Order.user_id == user_id).order_by(Order.id.desc()).all()

    def list_orders(self, offset: int, limit: int) -> list[Order]:
        return self.db.query(Order).order_by(Order.id.desc()).offset(offset).limit(limit).all()

    def save_order(
        self,
        user_id: int,
        item_id: int,
        quantity: int,
        new_balance: float,
        total_cost: float,
        recipient: dict | None = None,
    ) -> int:
        """
        Списание средств и создание записи заказа в одной транзакции:
        либо выполняются обе операции, либо ни одной (как в MVP-версии).
        Возвращает идентификатор созданного заказа.
        """
        self.db.query(User).filter(User.id == user_id).update({User.balance: new_balance})
        new_order = Order(
            user_id=user_id,
            item_id=item_id,
            quantity=quantity,
            status="processing",
            total_cost=total_cost,
            **({"recipient_" + field: value for field, value in recipient.items()} if recipient else {}),
        )
        self.db.add(new_order)
        self.db.commit()
        self.db.refresh(new_order)
        return new_order.id

    def advance_kitchen(self, order_id, status):
        try:
            # Shared with consumers/delivery: order lock comes before projection locks.
            order = self.db.get(Order, order_id, with_for_update=True, populate_existing=True)
            if order is None:
                raise NotFoundError("Заказ не найден")
            if order.status != "paid":
                raise ConflictError("Кухня работает только с оплаченным заказом", "kitchen_order_unpaid")
            row = self.db.query(KitchenOrder).filter_by(order_id=order_id).first()
            if row is None:
                row = KitchenOrder(order_id=order_id, status="pending")
                self.db.add(row)
            allowed = {"pending": "cooking", "cooking": "ready"}
            if row.status != status and allowed.get(row.status) != status:
                raise ConflictError("Недопустимый переход кухни", "invalid_kitchen_transition")
            row.status = status
            self.db.commit()
            return {"order_id": order_id, "status": order.status, "kitchen_status": row.status}
        except Exception:
            self.db.rollback()
            raise

    def pay_order(self, order_id: int) -> bool:
        """Статус и outbox атомарны. Повторная оплата не списывает деньги."""
        try:
            updated = (
                self.db.query(Order)
                .filter(Order.id == order_id, Order.status == "processing")
                .update({Order.status: "paid"}, synchronize_session=False)
            )
            if updated != 1:
                self.db.rollback()
                order = self.db.get(Order, order_id)
                return order is not None and order.status == "paid"
            order = self.db.get(Order, order_id, populate_existing=True)
            event = append_event(self.db, "order.paid", order.id, order.user_id, order.total_cost)
            self.db.commit()
            logger.info("order_paid_outbox_committed", extra=event_log_fields(event))
            return True
        except Exception:
            self.db.rollback()
            raise

    def cancel_order(self, order_id: int, expected_status: str, user_id: int, refund: float) -> bool:
        """
        Отмена заказа и возврат денег в одной транзакции.
        Статус меняется, только если он всё ещё равен expected_status — тому,
        который видел сервис. Два одновременных запроса на отмену не вернут
        деньги дважды: второй получит rowcount == 0 и False.
        """
        updated = (
            self.db.query(Order)
            .filter(Order.id == order_id, Order.status == expected_status)
            .update({Order.status: "cancelled"})
        )
        if updated != 1:
            self.db.rollback()
            return False
        self.db.query(User).filter(User.id == user_id).update({User.balance: User.balance + refund})
        self.db.commit()
        return True

    def get_stats(self, since: datetime, until: datetime) -> dict:
        """
        Число заказов по статусам и выручка за период [since, until) (UTC).
        Отменённые заказы в выручку не входят: деньги за них возвращены.
        """
        rows = (
            self.db.query(Order.status, func.count(Order.id), func.coalesce(func.sum(Order.total_cost), 0))
            .filter(Order.created_at >= since, Order.created_at < until)
            .group_by(Order.status)
            .all()
        )
        by_status = {status: count for status, count, _ in rows}
        revenue = sum(total for status, _, total in rows if status != "cancelled")
        return {"total": sum(by_status.values()), "revenue": float(revenue), "by_status": by_status}
