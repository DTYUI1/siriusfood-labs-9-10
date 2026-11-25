"""Read-only projection of approved admin fields in the existing shared database."""
from app.core.identity import RoleName
from app.models import Role, User
from app.modules.delivery.read_repository import DeliveryReadRepository


class AdminDeliveryRepository:
    def __init__(self, db):
        self.db = db

    def couriers(self, offset, limit):
        rows = self.db.query(User).join(Role).filter(Role.name == RoleName.COURIER).order_by(User.id).offset(offset).limit(limit).all()
        # Public handle: the current user schema has no username; no PII is disclosed.
        return [{"id": row.id, "username": f"Курьер №{row.id}"} for row in rows]

    def delivery_view(self, delivery, identity):
        return DeliveryReadRepository(self.db).view(delivery, identity)
