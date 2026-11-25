# SiriusFood · CI заказов

Учебный срез FastAPI-приложения SiriusFood для защиты практических работ №9 и №10. Исходники обработчиков заказов, событий и SQL-миграций взяты из проекта. Тесты запускаются на push и pull request.

Для локальной проверки нужны Python 3.12 и работающий Docker. Из корня репозитория:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -v --ignore=tests/integration
python -m pytest -v tests/integration
```

Компонентные тесты проверяют заказ, события и HTTP-метрики. Для каждого интеграционного теста Testcontainers поднимает чистую PostgreSQL 15 на случайном локальном порту. Alembic накатывает схему. После двух запросов подтверждения оплаты тест читает строки базы и сверяет один outbox, исходный correlation ID и сохранённый баланс. Учебные данные создаются внутри теста.
