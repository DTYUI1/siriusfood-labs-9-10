"""Fresh public SQL snapshot; personal/user/order tables are never read here."""
from dataclasses import asdict
from sqlalchemy.orm import Session
from app.modules.menu.repository import MenuRepository


class PublicMenuSource:
    def __init__(self, db: Session):
        self.db = db

    def items(self):
        # Deliberately bypass cache for projection freshness after edits.
        return [asdict(item) for item in MenuRepository(self.db, None)._load_items()]
