# ai_backends.py — клиенты и диспетчер инференса (OpenAI / локальный сервер).
#
# INFERENCE_BACKEND=openai (по умолчанию) — весь ответ, включая решение по тулам
#   (change_activity/update_profile_data), генерирует OpenAI, как и раньше.
# INFERENCE_BACKEND=local — текст реплики генерирует локальный сервер (serve_model.py),
#   а решение по тулам пока всё равно идёт через OpenAI (TOOL_DECISION_MODEL).
#   Это предположение, а не факт — умеет ли LoRA в tool calling нативно, проверяется
#   отдельным тестом на GPU-железе (см. план); decide_tools() может стать переключаемой
#   позже, если тест это подтвердит.

import asyncio
import os

from dotenv import load_dotenv
import httpx
from openai import AsyncOpenAI

load_dotenv()

OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

INFERENCE_BACKEND = os.getenv('INFERENCE_BACKEND', 'openai')
LOCAL_MODEL_URL = os.getenv('LOCAL_MODEL_URL', 'http://127.0.0.1:8008/reply')
TOOL_DECISION_MODEL = os.getenv('TOOL_DECISION_MODEL', 'gpt-4o')
LOCAL_MODEL_TIMEOUT = float(os.getenv('LOCAL_MODEL_TIMEOUT', '60'))
LOCAL_TEMPERATURE = float(os.getenv('LOCAL_TEMPERATURE', '0.6'))
LOCAL_TOP_P = float(os.getenv('LOCAL_TOP_P', '0.8'))
LOCAL_MAX_NEW_TOKENS = int(os.getenv('LOCAL_MAX_NEW_TOKENS', '150'))

http_client = httpx.AsyncClient(
    trust_env=True,  # Подтягивает настройки системного VPN/прокси Windows
    timeout=60.0     # Увеличиваем таймаут, чтобы VPN успевал ответить
)

ai_client = AsyncOpenAI(
    api_key=OPENAI_API_KEY,
    http_client=http_client
)

# Локальный сервер всегда на 127.0.0.1 — системный прокси/VPN тут не нужен и может всё сломать
local_http_client = httpx.AsyncClient(trust_env=False, timeout=LOCAL_MODEL_TIMEOUT)

# Описание инструментов для OpenAI
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "update_profile_data",
            "description": "Обновляет структурированный профиль клиента (факты, боли, рекомендации) и статус готовности к покупке.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Краткая сводка беседы с клиентом (2-3 предложения)."
                    },
                    "status": {
                        "type": "string",
                        "enum": ["new", "cold", "warm", "ready", "rejected"],
                        "description": "Текущий статус готовности клиента к покупке."
                    },
                    "name": {
                        "type": ["string", "null"],
                        "description": "Имя собеседника, если было явно озвучено."
                    },
                    "age": {
                        "type": ["integer", "null"],
                        "description": "Возраст собеседника, если был явно озвучен."
                    },
                    "location": {
                        "type": ["string", "null"],
                        "description": "Город/страна собеседника, если известны."
                    },
                    "occupation": {
                        "type": ["string", "null"],
                        "description": "Род занятий/работа собеседника, если известны."
                    },
                    "financial_status": {
                        "type": ["string", "null"],
                        "description": "Краткая оценка уровня дохода и отношения к деньгам."
                    },
                    "pain_points": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Финансовые и личные боли собеседника. Пустой массив, если ничего не выявлено."
                    },
                    "personal_facts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Бытовые факты о собеседнике (машина, животные, хобби и т.д.). Пустой массив, если ничего не выявлено."
                    },
                    "next_suggested_topic": {
                        "type": ["string", "null"],
                        "description": "Рекомендация, какую тему поднять в следующем сообщении."
                    }
                },
                "required": [
                    "summary", "status", "name", "age", "location", "occupation",
                    "financial_status", "pain_points", "personal_facts", "next_suggested_topic"
                ],
                "additionalProperties": False  # Для использования с strict=True
            },
            "strict": True
        }
    },
    {
        "type": "function",
        "function": {
            "name": "change_activity",
            "description": "Используй, если идешь спать, работать, гулять или в душ. Блокирует твои ответы.",
            "parameters": {
                "type": "object",
                "properties": {
                    "activity": {
                        "type": "string",
                        "description": "Что ты идешь делать (например: 'сон', 'душ', 'работа')."
                    },
                    "minutes": {
                        "type": "integer",
                        "description": "На сколько минут ты уходишь. Для сна ставь 480-600, для душа 20-40."
                    },
                    "promise_to_return": {
                        "type": "boolean",
                        "description": "True - если ты хочешь написать первой после возвращения (например, 'я вышла из душа' или утреннее 'доброе утро'). False - если диалог логически завершен и писать первой не надо."
                    }
                },
                "required": ["activity", "minutes", "promise_to_return"]
            }
        }
    }
]


async def decide_tools(messages_for_ai):
    """Решает, нужно ли вызвать change_activity/update_profile_data. Сейчас всегда через OpenAI —
    после теста на GPU-железе может стать диспетчером по бэкенду, как generate_reply()."""
    response = await ai_client.chat.completions.create(
        model=TOOL_DECISION_MODEL,
        messages=messages_for_ai,
        tools=TOOLS,
        max_tokens=60,
    )
    return response.choices[0].message.tool_calls


async def generate_reply_openai(messages_for_ai):
    response = await ai_client.chat.completions.create(
        model="gpt-4o",
        messages=messages_for_ai,
        tools=TOOLS,
        max_tokens=150,
    )
    msg = response.choices[0].message
    return msg.content, msg.tool_calls


async def _post_local_server(dynamic_extra, history_pairs):
    response = await local_http_client.post(
        LOCAL_MODEL_URL,
        json={
            "messages": history_pairs,
            "system_extra": dynamic_extra,
            "temperature": LOCAL_TEMPERATURE,
            "top_p": LOCAL_TOP_P,
            "max_new_tokens": LOCAL_MAX_NEW_TOKENS,
        },
    )
    response.raise_for_status()
    return response.json()["reply"]


async def generate_reply_local(messages_for_ai, dynamic_extra, history_pairs):
    """Параллельно: текст реплики — с локального сервера, решение по тулам — с OpenAI.
    Сбой decide_tools не должен ронять уже сгенерированный текст; сбой самого локального
    сервера — фатален, отвечать нечем."""
    tool_calls_result, local_result = await asyncio.gather(
        decide_tools(messages_for_ai),
        _post_local_server(dynamic_extra, history_pairs),
        return_exceptions=True,
    )

    if isinstance(local_result, Exception):
        raise local_result

    if isinstance(tool_calls_result, Exception):
        print(f"Ошибка decide_tools (локальный бэкенд): {tool_calls_result}")
        return local_result, None, True

    return local_result, tool_calls_result, False


async def generate_reply(messages_for_ai, dynamic_extra, history_pairs):
    """Диспетчер по INFERENCE_BACKEND. Возвращает (content, tool_calls, tool_decision_failed)."""
    if INFERENCE_BACKEND == "local":
        return await generate_reply_local(messages_for_ai, dynamic_extra, history_pairs)
    content, tool_calls = await generate_reply_openai(messages_for_ai)
    return content, tool_calls, False
