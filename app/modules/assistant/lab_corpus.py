"""Curated public facts for the lab assistant and its source links."""

from app.modules.assistant.corpus import RULES


def project_source(topic: str, title: str, text: str) -> dict:
    source_id = f"project-{topic}"
    return {"id": source_id, "title": title, "url": f"/assistant/sources/{source_id}", "text": text}


LAB_SOURCES = {
    **RULES,
    **{
        source["id"]: source
        for source in [
            project_source(
                "overview",
                "Что такое SiriusFood",
                "SiriusFood — учебный сервис заказа еды. Студент выбирает блюдо из меню, "
                "оформляет заказ и подтверждает оплату. Повар готовит заказ, курьер забирает "
                "и доставляет его. Администратор управляет меню, категориями и аккаунтами.",
            ),
            project_source(
                "architecture",
                "Как устроен SiriusFood",
                "Интерфейс работает с HTTP API на FastAPI. Меню, заказы и балансы хранятся "
                "в PostgreSQL. Redis используется для кеша и ограничения частоты запросов. "
                "Доставка работает отдельным gRPC-сервисом. Кухня и доставка получают "
                "события через RabbitMQ.",
            ),
            project_source(
                "roles",
                "Роли в SiriusFood",
                "Студент видит меню и свои заказы, оформляет заказ и подтверждает оплату. "
                "Повар редактирует меню и работает с заказами на кухне. "
                "Курьер видит и обновляет назначенные ему доставки. "
                "Администратор управляет меню, ролями, аккаунтами, заказами и доставками.",
            ),
            project_source(
                "events",
                "Оплата и события",
                "При подтверждении оплаты система сохраняет событие OrderPaid вместе со "
                "статусом paid. Outbox-воркер отправляет его в RabbitMQ. Кухня и доставка "
                "обрабатывают свои копии. Если свободного курьера нет, запускается "
                "компенсация с возвратом сохранённой суммы.",
            ),
        ]
    },
}
