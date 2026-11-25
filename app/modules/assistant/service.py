"""Informational only: local owner reads or grounded public generation."""
import uuid
from app.exceptions import AccessDeniedError
from app.modules.assistant.corpus import public_documents
from app.modules.assistant.privacy import classify


class AssistantService:
    def __init__(self, menu_source, own_order_read, retrieval, generation_factory):
        self.menu_source, self.own_order_read = menu_source, own_order_read
        self.retrieval, self.generation_factory = retrieval, generation_factory

    def chat(self, message, identity):
        request_id = uuid.uuid4().hex
        menu = self.menu_source.items()
        intent = classify(message, menu)
        def response(answer, mode, sources=None):
            return {"answer": answer, "sources": sources or [], "mode": mode, "request_id": request_id}
        if intent.kind == "private":
            return response("Личные данные и произвольные инструкции не передаются внешней модели. Посмотри свой профиль или задай общий вопрос о меню и правилах.", "local")
        if intent.kind in ("own_order", "own_history"):
            orders = self.own_order_read.own_orders(identity)
            if intent.order_id is not None:
                orders = [order for order in orders if order.id == intent.order_id]
                if not orders:
                    raise AccessDeniedError("Заказ недоступен")
            if not orders:
                return response("У тебя пока нет заказов.", "local")
            if intent.kind == "own_history":
                lines = [f"№{order.id}: {order.status}, {order.total_cost:g} ₽" for order in orders[:5]]
                return response("Твои последние заказы: " + "; ".join(lines) + ". Это локальные сведения; помощник не создаёт повторный заказ.", "local")
            order = orders[0]
            return response(f"Твой заказ №{order.id}: статус {order.status}, сохранённая сумма {order.total_cost:g} ₽. Это локальные сведения; помощник не меняет заказ.", "local")
        if intent.kind == "no_evidence":
            return response(intent.clarification or "В подтверждённых источниках нет ответа. Я могу объяснить меню, цены, оплату, отмену и общие правила доставки.", "no_evidence")
        docs = public_documents(menu)
        evidence_by_id = {}
        for query in intent.queries:
            found = self.retrieval.search(query.question, docs, query.source_ids)
            found = [doc for doc in found if doc.get('id') in query.source_ids]
            if not found or (query.require_all and not set(query.source_ids).issubset({d['id'] for d in found})):
                return response("Не найдено достаточно подтверждённых источников для каждой части вопроса. Уточни вопрос.", "no_evidence")
            evidence_by_id.update((doc['id'], doc) for doc in found)
        evidence = list(evidence_by_id.values())
        # Sanitized canonical question + allowlisted public facts only; no message/identity/order.
        result = self.generation_factory().generate(intent.question, [
            {key: doc[key] for key in ('id', 'title', 'text')} for doc in evidence])
        by_id = {doc['id']: doc for doc in evidence}
        cited = set(result.source_ids)
        if not cited.issubset(by_id) or any(
            (not cited.intersection(query.source_ids)) or
            (query.require_all and not set(query.source_ids).issubset(cited))
            for query in intent.queries
        ):
            return response("Ответ не подтвердил источниками каждую запрошенную часть. Уточни вопрос.", "no_evidence")
        sources = [{key: by_id[source_id][key] for key in ('id', 'title', 'url')} for source_id in result.source_ids]
        return response(result.answer, "generated", sources)
