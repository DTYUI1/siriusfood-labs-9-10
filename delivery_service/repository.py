"""
Хранилище доставок на SQLAlchemy (реализация порта DeliveryRepository).
Бизнес-правил здесь нет.
"""
from sqlalchemy.orm import Session

from delivery_service.domain import (
    ACTIVE_STATUSES, CourierInfo, DeliveryRecord, DeliveryRepository,
    OrderInfo, RuleViolation, STATUS_COMPLETED, STATUS_ON_THE_WAY, TransitionConflict, Forbidden,
)
from delivery_service.models import Delivery, KitchenOrder, Order, User


class SqlAlchemyDeliveryRepository(DeliveryRepository):
    def __init__(self, db: Session):
        self.db = db

    def get_order(self, order_id: int) -> OrderInfo | None:
        order = self.db.get(Order, order_id)
        return OrderInfo(order.id, order.user_id, order.status) if order else None

    def get_user(self, user_id: int) -> CourierInfo | None:
        user = self.db.get(User, user_id)
        if user is None:
            return None
        return CourierInfo(user.id, user.role.name if user.role else "", bool(user.is_blocked))

    def get(self, delivery_id: int) -> DeliveryRecord | None:
        delivery = self.db.get(Delivery, delivery_id)
        return self._to_record(delivery) if delivery else None

    def list_by_courier(self, courier_id: int, statuses: tuple[str, ...] | None = None) -> list[DeliveryRecord]:
        query = self.db.query(Delivery).filter(Delivery.courier_id == courier_id)
        if statuses is not None:
            query = query.filter(Delivery.status.in_(statuses))
        return [self._to_record(delivery) for delivery in query.order_by(Delivery.id)]

    def list_deliveries(self, offset: int, limit: int) -> list[DeliveryRecord]:
        rows = self.db.query(Delivery).order_by(Delivery.id.desc()).offset(offset).limit(limit).all()
        return [self._to_record(row) for row in rows]

    def add(self, order_id: int, courier_id: int, status: str) -> int:
        # Те же блокировки, что у event consumer: ручной gRPC-вызов не обходит
        # правило «один курьер — одна активная доставка» при гонке.
        try:
            order = self.db.get(Order, order_id, with_for_update=True, populate_existing=True)
            if order is None or order.status != "paid":
                raise RuleViolation("Курьера можно назначить только на оплаченный заказ")
            courier = self.db.get(User, courier_id, with_for_update=True, populate_existing=True)
            if courier is None or courier.is_blocked or courier.role is None or courier.role.name != "courier":
                raise Forbidden("Курьер больше недоступен для назначения")
            if self.db.query(Delivery).filter_by(order_id=order_id).first():
                raise RuleViolation("Для заказа уже создана доставка")
            if self.list_by_courier(courier_id, ACTIVE_STATUSES):
                raise RuleViolation("У курьера уже есть активная доставка")
            delivery = Delivery(order_id=order_id, courier_id=courier_id, status=status)
            self.db.add(delivery)
            self.db.commit()
            return delivery.id
        except Exception:
            self.db.rollback()
            raise

    def change_status(self, delivery_id: int, expected: tuple[str, ...], new_status: str) -> bool:
        try:
            initial = self.db.get(Delivery, delivery_id)
            if initial is None:
                return False
            order = self.db.get(Order, initial.order_id, with_for_update=True, populate_existing=True)
            delivery = self.db.get(Delivery, delivery_id, with_for_update=True, populate_existing=True)
            if delivery.status not in expected or order is None:
                self.db.rollback()
                return False
            if order.status != "paid":
                raise TransitionConflict("Заказ больше недоступен для доставки")
            if new_status == STATUS_ON_THE_WAY:
                recipient = (order.recipient_name, order.recipient_phone, order.recipient_location)
                if any(recipient):
                    kitchen = self.db.query(KitchenOrder).filter_by(order_id=order.id).first()
                    if not all(recipient) or kitchen is None or kitchen.status != "ready":
                        raise TransitionConflict("Заказ ещё не готов к выдаче")
            delivery.status = new_status
            if new_status == STATUS_COMPLETED:
                order.status = "delivered"
            self.db.commit()
            return True
        except Exception:
            self.db.rollback()
            raise

    @staticmethod
    def _to_record(delivery: Delivery) -> DeliveryRecord:
        return DeliveryRecord(
            id=delivery.id,
            order_id=delivery.order_id,
            courier_id=delivery.courier_id,
            customer_id=delivery.order.user_id if delivery.order else None,
            status=delivery.status,
        )
