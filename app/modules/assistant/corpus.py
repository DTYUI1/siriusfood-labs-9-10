"""Approved public corpus only. No user, order, identity or dialogue data."""
RULES = {
    "rules-payment": {"id": "rules-payment", "title": "Оформление и подтверждение оплаты",
        "url": "/assistant/sources/rules-payment",
        "text": "Студент оформляет одно блюдо с количеством. Цена берётся сервером из меню. Деньги списываются при создании заказа. Подтверждение оплаты переводит processing в paid и второй раз деньги не списывает."},
    "rules-cancel": {"id": "rules-cancel", "title": "Отмена заказа",
        "url": "/assistant/sources/rules-cancel",
        "text": "Владелец может отменить заказ только в processing, до подтверждения оплаты. Возвращается сохранённая сумма один раз. После paid пользовательская отмена недоступна. Помощник сам не отменяет заказы."},
    "rules-delivery": {"id": "rules-delivery", "title": "Доставка и её состояния",
        "url": "/assistant/sources/rules-delivery",
        "text": "После подтверждения оплаты система назначает свободного курьера. Если свободного курьера нет, запускается компенсация с возвратом. Назначенный курьер забирает и завершает доставку. Гарантированный срок доставки не установлен."},
}


def public_documents(menu):
    docs = list(RULES.values())
    for item in menu:
        docs.append({"id": f"menu-{item['id']}", "title": item["name"],
            "url": f"/menu/items#item-{item['id']}",
            "text": f"Блюдо: {item['name']}. Цена: {item['price']} ₽. Категория: {item['category_name'] or 'без категории'}. Доступно для заказа: {'да' if item['is_available'] else 'нет'}."})
    return docs
