"""Bounded conversation history, with one writer at a time per Redis key."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
import json
from secrets import token_urlsafe

from redis import Redis, RedisError, WatchError

from app.modules.assistant.lab_generation import MAX_ANSWER_LENGTH

CONVERSATION_ID_PATTERN = r"^[A-Za-z0-9_-]{43}$"
CONVERSATION_TTL = 30 * 60
LEASE_TTL = 120
HISTORY_TURNS = 6


class ConversationNotFound(Exception):
    """A supplied ID has no active conversation in this scope."""


class ConversationBusy(Exception):
    """Another request holds the conversation lease."""


class ConversationUnavailable(Exception):
    """The stored state or assistant result cannot be used safely."""


@dataclass(frozen=True)
class Conversation:
    id: str = field(repr=False)
    history: list[dict[str, str]] = field(repr=False)
    off_topic_count: int
    _key: str = field(repr=False)
    _lease: str = field(repr=False)
    _existing: bool

    @property
    def _lease_key(self):
        return f"{self._key}:lease"


class ConversationStore:
    def __init__(self, redis: Redis):
        self.redis = redis

    @contextmanager
    def open(self, owner: str, supplied_id: str | None):
        conversation_id = supplied_id or token_urlsafe(32)
        digest = sha256(f"{owner}\0{conversation_id}".encode()).hexdigest()
        key = f"assistant:conversation:v1:{digest}"
        lease_key = f"{key}:lease"
        lease = token_urlsafe(24)
        if not self.redis.set(lease_key, lease, nx=True, ex=LEASE_TTL):
            raise ConversationBusy
        try:
            raw = self.redis.get(key)
            if supplied_id is not None and raw is None:
                raise ConversationNotFound
            if raw is None:
                history, count = [], 0
            else:
                history, count = self._decode(raw)
            yield Conversation(conversation_id, history, count, key, lease, raw is not None)
        finally:
            self._release(lease_key, lease)

    def save(self, conversation: Conversation, message: str, answer: str, off_topic_count: int):
        if type(off_topic_count) is not int or not 0 <= off_topic_count <= 2:
            raise ConversationUnavailable
        history = [
            *conversation.history,
            {"role": "user", "content": message},
            {"role": "assistant", "content": answer[:MAX_ANSWER_LENGTH]},
        ][-2 * HISTORY_TURNS:]
        raw = json.dumps({"history": history, "off_topic_count": off_topic_count}, ensure_ascii=False)
        try:
            with self.redis.pipeline() as pipe:
                pipe.watch(conversation._lease_key, conversation._key)
                if pipe.get(conversation._lease_key) != conversation._lease:
                    raise ConversationBusy
                if conversation._existing and not pipe.exists(conversation._key):
                    raise ConversationNotFound
                pipe.multi()
                pipe.set(conversation._key, raw, ex=CONVERSATION_TTL)
                pipe.execute()
        except WatchError:
            raise ConversationBusy from None

    def _release(self, key: str, lease: str):
        # The lease can expire before the request finishes. Leave the next
        # owner's lease intact, and let Redis expire ours if it is unavailable.
        try:
            with self.redis.pipeline() as pipe:
                pipe.watch(key)
                if pipe.get(key) == lease:
                    pipe.multi()
                    pipe.delete(key)
                    pipe.execute()
        except RedisError:
            pass

    @staticmethod
    def _decode(raw: str):
        try:
            state = json.loads(raw)
            history = state["history"]
            count = state["off_topic_count"]
            if type(count) is not int or not 0 <= count <= 2:
                raise ValueError
            if not isinstance(history, list) or len(history) % 2 or len(history) > 2 * HISTORY_TURNS:
                raise ValueError
            for index, entry in enumerate(history):
                role = "user" if index % 2 == 0 else "assistant"
                if entry["role"] != role or not isinstance(entry["content"], str):
                    raise ValueError
        except (KeyError, IndexError, TypeError, ValueError):
            raise ConversationUnavailable from None
        return history, count
