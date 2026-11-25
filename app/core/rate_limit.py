"""
Ограничение частоты запросов (rate limiting) на Redis.

Счётчик хранится в Redis, а не в памяти процесса: при нескольких репликах
приложения у каждой был бы свой счётчик, и лимит умножался бы на их число.
"""
from fastapi import HTTPException, Request
from redis import RedisError

from app.core.redis import get_redis


def rate_limit(max_requests: int = 5, window: int = 60, scope: str | None = None):
    """
    Dependency: не больше max_requests запросов с одного IP за window секунд.
    scope — общее имя счётчика для нескольких эндпоинтов. Например, у /auth/login
    и /auth/token один счётчик, иначе через два адреса попыток было бы вдвое больше.
    Возбуждает 429 при превышении лимита и 503, если Redis недоступен.
    """

    def checker(request: Request) -> None:
        ip = request.client.host if request.client else "unknown"
        key = f"rate_limit:{scope or request.url.path}:{ip}"
        try:
            # INCR и EXPIRE в одной транзакции: ключ не останется без срока жизни,
            # даже если процесс упадёт между командами. NX — срок ставится только
            # первому запросу окна и не продлевается следующими.
            pipe = get_redis().pipeline()
            pipe.incr(key)
            pipe.expire(key, window, nx=True)
            pipe.ttl(key)
            current, _, ttl = pipe.execute()
        except RedisError as error:
            # Fail closed: без счётчика защита от перебора не работает,
            # поэтому запрос не пропускается.
            raise HTTPException(status_code=503, detail="Сервис временно недоступен") from error

        if current > max_requests:
            raise HTTPException(
                status_code=429,
                detail="Слишком много запросов, попробуйте позже",
                headers={"Retry-After": str(max(ttl, 1))},
            )

    return checker
