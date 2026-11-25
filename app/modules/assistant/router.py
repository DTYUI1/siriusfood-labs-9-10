import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, ConfigDict, Field
from redis import RedisError
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session
from app.core.dependencies import get_auth_service, get_current_user
from app.core.identity import CurrentUser, RoleName
from app.core.jwt import ACCESS, decode_token_identity
from app.core.product_config import product_settings
from app.core.rate_limit import rate_limit
from app.core.redis import get_redis
from app.database import get_db
from app.exceptions import DomainError
from app.http_errors import to_http_exception
from app.modules.assistant.conversations import (
    CONVERSATION_ID_PATTERN,
    ConversationBusy,
    ConversationNotFound,
    ConversationStore,
    ConversationUnavailable,
)
from app.modules.assistant.corpus import RULES
from app.modules.assistant.lab_corpus import LAB_SOURCES
from app.modules.assistant.lab_service import LabAssistant
from app.modules.assistant.mcp_client import MCPUnavailable
from app.modules.assistant.provider import BaiGeneration, ProviderUnavailable
from app.modules.assistant.retrieval import VectorRetrieval, RetrievalUnavailable
from app.modules.assistant.service import AssistantService
from app.modules.orders.repository import OrderRepository
from app.modules.orders.service import OrderService
from app.modules.search.repository import PublicMenuSource

router = APIRouter()
optional_bearer = OAuth2PasswordBearer(tokenUrl="/auth/token", auto_error=False)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=1000)
    conversation_id: str | None = Field(
        default=None, min_length=43, max_length=43, pattern=CONVERSATION_ID_PATTERN,
    )


def get_assistant(db: Session = Depends(get_db)):
    if lab_enabled():
        dsn = os.getenv("SIRIUS_LABS_ASSISTANT_READER_DSN", "")
        app_url = db.get_bind().url
        try:
            reader_url = make_url(dsn)
            if (
                reader_url.get_backend_name() != "postgresql"
                or app_url.get_backend_name() != "postgresql"
                or reader_url.host != app_url.host
                or (reader_url.port or 5432) != (app_url.port or 5432)
                or reader_url.database != app_url.database
                or reader_url.username == app_url.username
            ):
                dsn = ""
        except (ArgumentError, ValueError):
            dsn = ""
        return LabAssistant(dsn)
    cfg = product_settings()
    return AssistantService(PublicMenuSource(db), OrderService(OrderRepository(db)),
        VectorRetrieval(cfg.vector_dsn, cfg.embedding_path, cfg.embedding_revision),
        lambda: BaiGeneration(cfg.pricing_file, cfg.bai_secret_file, cfg.generation_purpose))


def lab_enabled():
    return os.getenv("SIRIUS_LABS_ASSISTANT") == "1"


def get_chat_identity(
    request: Request, token: str | None = Depends(optional_bearer), auth=Depends(get_auth_service)
) -> CurrentUser | None:
    if token:
        identity = get_current_user(token, auth)
        if not lab_enabled() and identity.role != RoleName.STUDENT:
            raise HTTPException(403, "Недостаточно прав для этого действия")
        return identity
    if not lab_enabled() or "authorization" in request.headers:
        raise HTTPException(401, "Требуется вход", headers={"WWW-Authenticate": "Bearer"})
    return None


@router.post('/chat', dependencies=[Depends(rate_limit(max_requests=10, window=60, scope="assistant"))])
def chat(
    payload: ChatRequest,
    response: Response,
    identity: CurrentUser | None = Depends(get_chat_identity),
    token: str | None = Depends(optional_bearer),
    service=Depends(get_assistant),
):
    if not payload.message.strip():
        raise HTTPException(422, "Пустой вопрос")
    response.headers["Cache-Control"] = "no-store"
    request_id = uuid.uuid4().hex
    try:
        if not lab_enabled():
            return service.chat(payload.message, identity)

        owner = "guest"
        if identity is not None:
            _, auth_version = decode_token_identity(token, ACCESS)
            owner = f"user:{identity.id}:{identity.role}:{auth_version}"
        store = ConversationStore(get_redis())
        with store.open(owner, payload.conversation_id) as conversation:
            result = service.chat(
                payload.message, identity, request_id,
                history=[entry.copy() for entry in conversation.history],
                off_topic_count=conversation.off_topic_count,
            )
            store.save(conversation, payload.message, result["answer"], result["off_topic_count"])
        return {
            "answer": result["answer"],
            "sources": result["sources"],
            "mode": result["mode"],
            "request_id": request_id,
            "conversation_id": conversation.id,
        }
    except ConversationNotFound:
        raise HTTPException(404, {
            "code": "assistant_conversation_not_found",
            "message": "Диалог закончился. Открой новый чат.",
            "request_id": request_id,
        }) from None
    except ConversationBusy:
        raise HTTPException(409, {
            "code": "assistant_conversation_busy",
            "message": "Чат ещё отвечает на предыдущий вопрос.",
            "request_id": request_id,
        }) from None
    except (ConversationUnavailable, RedisError):
        raise HTTPException(503, {
            "code": "assistant_conversation_unavailable",
            "message": "История чата временно недоступна.",
            "request_id": request_id,
        }) from None
    except DomainError as error:
        raise to_http_exception(error) from error
    except MCPUnavailable:
        raise HTTPException(503, {
            "code": "assistant_mcp_unavailable",
            "message": "Источник данных помощника временно недоступен.",
            "request_id": request_id,
        }) from None
    except RetrievalUnavailable:
        raise HTTPException(503, {"code": "retrieval_unavailable", "request_id": uuid.uuid4().hex}) from None
    except ProviderUnavailable as error:
        raise HTTPException(error.status, {"code": error.code, "request_id": uuid.uuid4().hex}) from None


@router.get('/sources/{source_id}')
def public_source(source_id: str):
    sources = LAB_SOURCES if lab_enabled() else RULES
    if source_id not in sources:
        raise HTTPException(404, "Источник не найден")
    return sources[source_id]
