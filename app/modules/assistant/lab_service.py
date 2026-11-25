"""Model-led SiriusFood chat over a bounded, local read-only MCP session."""

import logging
from time import monotonic
from app.core.identity import RoleName
from app.core.product_config import product_settings
from app.modules.assistant.lab_generation import (
    FINISH_CHOICE, FINISH_FUNCTION, FINISH_NAME, HistoryMessage, ModelAnswer, READ_ARGUMENTS_LIMIT,
    SYSTEM, ToolCall,
)
from app.modules.assistant.mcp_client import MCPClient
from app.modules.assistant.mcp_contract import MAX_CALLS, TOOLS, decode, encode
from app.modules.assistant.provider import BaiGeneration, ProviderUnavailable

logger = logging.getLogger(__name__)
MAX_STEPS = 4
TURN_SECONDS = 40
OFF_TOPIC_LIMIT = 2
CURRENT_MENU = {"id": "current-menu", "title": "Текущее меню", "url": "/menu/items"}


def _generation():
    cfg = product_settings()
    return BaiGeneration(cfg.pricing_file, cfg.bai_secret_file, cfg.generation_purpose)


def _function_tools(listed):
    names = {FINISH_NAME: FINISH_NAME}
    schemas = [FINISH_FUNCTION]
    for tool in listed:
        name = tool["name"].replace(".", "_")
        names[name] = tool["name"]
        schemas.append({"type": "function", "function": {
            "name": name, "description": tool["description"], "parameters": tool["inputSchema"],
        }})
    return names, schemas


def _validate_calls(raw_calls, names, seen_ids):
    parsed = []
    try:
        ids = set(seen_ids)
        for raw_call in raw_calls:
            call = ToolCall.model_validate(raw_call)
            if call.function.name not in names or call.id in ids:
                raise ValueError("invalid_tool_or_call_id")
            wire_name = names[call.function.name]
            raw_arguments = call.function.arguments.encode()
            if wire_name != FINISH_NAME and len(raw_arguments) > READ_ARGUMENTS_LIMIT:
                raise ValueError("read_arguments_limit")
            arguments = decode(raw_arguments)
            if wire_name != FINISH_NAME:
                arguments = TOOLS[wire_name][0].model_validate(arguments).model_dump(mode="json")
            ids.add(call.id)
            parsed.append((call.id, wire_name, arguments))
    except (ValueError, RecursionError):
        raise ProviderUnavailable("invalid_tool_call") from None
    return parsed


def _tool_result(name, result, sources):
    body = result.model_dump(mode="json")
    if name == "project.read":
        sources[result.id] = {key: body[key] for key in ("id", "title", "url")}
    if name == "menu.read":
        sources[CURRENT_MENU["id"]] = CURRENT_MENU
        body["source_id"] = CURRENT_MENU["id"]
    return encode(body).decode()


def _parse_json_answer(content):
    if not isinstance(content, str):
        raise ValueError("missing_model_answer")
    text = content.strip()
    header, _, body = text.partition("\n")
    if header in ("```json", "```") and body.endswith("```"):
        text = body[:-3].strip()
    return decode(text.encode())


def _finish(payload, count, request_id, sources, read_count):
    try:
        answer = ModelAnswer.model_validate(payload)
    except ValueError:
        raise ProviderUnavailable("invalid_model_answer") from None
    if answer.kind == "domain":
        count = 0
    elif answer.kind in ("casual", "redirect"):
        count = min(count + 1, OFF_TOPIC_LIMIT)
    logger.info("assistant_model request_id=%s kind=%s mcp_calls=%d", request_id, answer.kind, read_count)
    return {
        "answer": answer.answer,
        "sources": [sources[source_id] for source_id in dict.fromkeys(answer.source_ids) if source_id in sources],
        "mode": "mcp" if read_count else "generated",
        "request_id": request_id,
        "off_topic_count": count,
    }


class LabAssistant:
    def __init__(self, reader_dsn, generation_factory=None):
        self.reader_dsn = reader_dsn
        self.generation_factory = generation_factory or _generation

    def chat(self, message, identity, request_id, *, history=(), off_topic_count=0):
        if type(off_topic_count) is not int or not 0 <= off_topic_count <= OFF_TOPIC_LIMIT:
            raise ValueError("invalid off-topic count")
        if len(history) % 2:
            raise ValueError("expected complete dialogue turns")
        previous = [HistoryMessage.model_validate(entry).model_dump() for entry in history]
        if any(entry["role"] != ("assistant" if index % 2 else "user") for index, entry in enumerate(previous)):
            raise ValueError("invalid dialogue roles")
        student_id = identity.id if identity is not None and identity.role == RoleName.STUDENT else None
        scope = "Инструменты студента доступны." if student_id is not None else "Доступны только публичные инструменты."
        system_message = {  # Добавляем к промпту права текущего пользователя и счётчик вопросов вне темы.
            "role": "system",
            "content": f"{SYSTEM}\nСчётчик коротких ответов не по теме: {off_topic_count}/{OFF_TOPIC_LIMIT}. {scope}",
        }
        user_message = {"role": "user", "content": message}
        deadline = monotonic() + TURN_SECONDS
        generation = self.generation_factory()
        sources, seen_ids, transcript = {}, set(), []
        read_count = 0
        needs_envelope = False
        with MCPClient(self.reader_dsn, student_id=student_id) as mcp:
            names, tools = _function_tools(mcp.listed)
            for step in range(MAX_STEPS):
                finish_only = needs_envelope or step == MAX_STEPS - 1 or read_count == MAX_CALLS
                choice = FINISH_CHOICE if finish_only else "auto"
                model_messages = [system_message, *previous, user_message, *transcript]
                while previous and generation.tool_context_size(model_messages, tools, choice) > generation.TOOL_INPUT_LIMIT:
                    del previous[:2]
                    del model_messages[1:3]
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise ProviderUnavailable("assistant_timeout", 504)
                # Через b.ai модель получает историю, вопрос и описание разрешённых инструментов.
                response = generation.tool_step(
                    model_messages, tools, choice=choice, timeout=min(generation.DEADLINE, remaining),
                )
                raw_calls = response.get("tool_calls", [])
                if not raw_calls:
                    try:
                        payload = _parse_json_answer(response["content"])
                        return _finish(payload, off_topic_count, request_id, sources, read_count)
                    except (ValueError, RecursionError, ProviderUnavailable):
                        if finish_only:
                            raise ProviderUnavailable("invalid_model_answer") from None
                        needs_envelope = True
                        continue
                calls = _validate_calls(raw_calls, names, seen_ids)
                if len(calls) == 1 and calls[0][1] == FINISH_NAME:
                    return _finish(calls[0][2], off_topic_count, request_id, sources, read_count)
                if finish_only or read_count + sum(name != FINISH_NAME for _, name, _ in calls) > MAX_CALLS:
                    raise ProviderUnavailable("assistant_tool_limit")
                transcript.append(response)
                for call_id, wire_name, arguments in calls:
                    if monotonic() >= deadline:
                        raise ProviderUnavailable("assistant_timeout", 504)
                    if wire_name == FINISH_NAME:
                        body = '{"status":"finish_after_read_results"}'
                    else:
                        # Сервер выполняет выбранное чтение и возвращает данные модели для ответа.
                        body = _tool_result(wire_name, mcp.call(wire_name, arguments), sources)
                        read_count += 1
                    seen_ids.add(call_id)
                    transcript.append({"role": "tool", "tool_call_id": call_id, "content": body})
        raise ProviderUnavailable("assistant_tool_limit")
