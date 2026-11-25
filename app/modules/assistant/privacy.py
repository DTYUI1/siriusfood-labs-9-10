"""Construct public questions from an allowlist, never forward arbitrary raw text."""
from dataclasses import dataclass
import re
from app.modules.assistant.semantics import plan_public_question, compile_public_question


@dataclass(frozen=True)
class Intent:
    kind: str
    question: str = ""
    source_ids: tuple[str, ...] = ()
    order_id: int | None = None
    queries: tuple = ()
    clarification: str = ""


def classify(message: str, menu: list[dict]) -> Intent:
    text = message.casefold().replace('ё', 'е')
    # These checks happen before retrieval, embeddings, and external generation.
    private = re.search(r"@|https?://|\d{5,}|парол|токен|jwt|secret|секрет|телефон|адрес|email|e-mail|почт|баланс|паспорт|получател", text)
    injection = re.search(r"ignore|system|developer|prompt|игнорир|обойди|выдай ключ", text)
    instruction = "инструкц" in text
    benign_instruction = re.search(r"инструкц[а-я]*\s+по\s+(?:оплат|оформл|отмен|достав)", text)
    override = re.search(r"забуд|предыдущ|нов[а-я]*\s+инструкц|следуй|вместо|переопред", text)
    if private or injection or (instruction and (not benign_instruction or override)):
        return Intent("private")
    if re.search(r"(?:что|какие).{0,12}я.{0,12}заказывал|истори.{0,8}заказ|повторить.{0,12}(?:мой|последний).{0,8}заказ", text):
        return Intent("own_history")
    order = re.search(r"заказ(?:а|у|ом|ы|ов)?\s*(?:№|#)?\s*(\d+)", text)
    if order:
        return Intent("own_order", order_id=int(order.group(1)))
    if re.search(r"мо[йияею].{0,20}заказ|заказ.{0,20}мо[йияею]", text):
        return Intent("own_order")
    if re.search(r"\bу\s+меня\b.{0,20}\bзаказ(?:ы|ов|а)?\b|\bзаказ(?:ы|ов|а)?\b.{0,20}\bу\s+меня\b", text):
        return Intent("own_order")
    if re.search(r"состав|ингредиент|аллерг|грамм|\bвес(?:а|ом|ит)?\b|калор|бжу|глютен|лактоз|орех|диет|веган|вегетар|остаток|остатки|запас|срок", text):
        return Intent("no_evidence")
    plan = plan_public_question(text, menu)
    compiled = compile_public_question(plan, menu)
    if not compiled.queries:
        return Intent("no_evidence", clarification=compiled.clarification)
    return Intent("public", compiled.question, compiled.source_ids,
                  queries=compiled.queries)
