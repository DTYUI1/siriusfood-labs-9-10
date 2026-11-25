"""
Комнаты чата «заказчик — курьер» для ChatWithCourier.

Комната — одна доставка. Сообщение участника рассылается всем подключённым
к комнате, в том числе отправителю: так он видит время, выставленное сервером.
Хранится в памяти процесса: при перезапуске сервиса история пропадает,
а при нескольких экземплярах сервиса участники разных экземпляров друг друга
не услышат — для этого нужна общая шина (Redis Pub/Sub).
"""
import queue
import threading
from collections import deque
from dataclasses import dataclass, field

HISTORY_SIZE = 50
# Отключение участника: сигнал генератору ответа закончить поток.
CLOSED = object()


@dataclass
class _Room:
    history: deque = field(default_factory=lambda: deque(maxlen=HISTORY_SIZE))
    subscribers: set = field(default_factory=set)


class ChatHub:
    def __init__(self):
        self._lock = threading.Lock()
        self._rooms: dict[int, _Room] = {}

    def join(self, delivery_id: int) -> tuple[queue.Queue, list]:
        """Подписка на комнату. Возвращает очередь новых сообщений и историю."""
        inbox: queue.Queue = queue.Queue()
        with self._lock:
            room = self._rooms.setdefault(delivery_id, _Room())
            room.subscribers.add(inbox)
            return inbox, list(room.history)

    def leave(self, delivery_id: int, inbox: queue.Queue) -> None:
        with self._lock:
            room = self._rooms.get(delivery_id)
            if room is not None:
                room.subscribers.discard(inbox)

    def publish(self, delivery_id: int, message) -> None:
        with self._lock:
            room = self._rooms.setdefault(delivery_id, _Room())
            room.history.append(message)
            subscribers = list(room.subscribers)
        for inbox in subscribers:
            inbox.put(message)
