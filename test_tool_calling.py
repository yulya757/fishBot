# test_tool_calling.py — сравнение tool calling: локальная LoRA (через vLLM) vs OpenAI-бейзлайн.
#
# Запуск (после того, как поднят `vllm serve ... --port 8010`, см. RUNBOOK_LOCAL_INFERENCE.md,
# шаг «Тест: умеет ли локальная модель в tool calling»):
#   python test_tool_calling.py
#
# Бейзлайн через OpenAI (decide_tools из ai_backends.py) запускается только если в .env
# заполнен OPENAI_API_KEY — иначе просто выводится результат vLLM без сравнения.

import asyncio
import json

from openai import OpenAI

from ai_backends import OPENAI_API_KEY, TOOLS, decide_tools

VLLM_BASE_URL = "http://127.0.0.1:8010/v1"
VLLM_MODEL = "nastya"  # имя из --lora-modules nastya=lora_v2

with open("system_promt.txt", "r", encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read().strip()

# (реплика, ожидание) — ожидание только для человека, скрипт его не проверяет автоматически
CASES = [
    ("спокойной ночи, я спать", "change_activity: сон, minutes 480-600"),
    ("всё, я в душ, потом напишу", "change_activity: душ, minutes 20-40, promise_to_return=true"),
    ("извини, побежала, дела", "change_activity: работа/дела, promise_to_return по контексту"),
    ("ладно, давай, научи меня как это работает", "update_profile_data: status=ready"),
    ("скинь мне ссылку/инструкцию, что делать дальше", "update_profile_data: status=ready"),
    ("это разводка, вы мошенники, не пиши мне больше", "update_profile_data: status=rejected"),
    ("ой, интересно, а как ты вообще этим занимаешься?", "update_profile_data: status=warm"),
    ("привет, как дела?", "тул НЕ должен вызываться"),
    ("а ты откуда сама?", "тул НЕ должен вызываться"),
    ("ахах, смешно) а сама чем занимаешься по жизни", "тул НЕ должен вызываться"),
    ("сколько тебе лет?", "тул НЕ должен вызываться"),
    ("норм, погнали дальше", "тул НЕ должен вызываться"),
]

vllm_client = OpenAI(base_url=VLLM_BASE_URL, api_key="not-needed")


def call_vllm(user_text):
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]
    response = vllm_client.chat.completions.create(
        model=VLLM_MODEL,
        messages=messages,
        tools=TOOLS,
        tool_choice="auto",
        max_tokens=150,
    )
    msg = response.choices[0].message
    return msg.tool_calls, msg.content


def format_tool_calls(tool_calls):
    if not tool_calls:
        return "(тул не вызван)"
    parts = []
    for tc in tool_calls:
        try:
            args = json.loads(tc.function.arguments)
        except (json.JSONDecodeError, TypeError):
            args = tc.function.arguments
        parts.append(f"{tc.function.name}({args})")
    return "; ".join(parts)


async def call_openai_baseline(user_text):
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]
    return await decide_tools(messages)


async def main():
    have_baseline = bool(OPENAI_API_KEY)
    if not have_baseline:
        print("OPENAI_API_KEY не задан в .env — бейзлайн пропущен, покажу только vLLM.\n")

    for user_text, expectation in CASES:
        print(f"> {user_text}")
        print(f"  ожидание: {expectation}")

        try:
            vllm_calls, vllm_content = call_vllm(user_text)
            print(f"  vLLM:     {format_tool_calls(vllm_calls)}"
                  + (f"  | текст: {vllm_content!r}" if vllm_content else ""))
        except Exception as e:
            print(f"  vLLM:     ОШИБКА — {e!r}")

        if have_baseline:
            try:
                openai_calls = await call_openai_baseline(user_text)
                print(f"  OpenAI:   {format_tool_calls(openai_calls)}")
            except Exception as e:
                print(f"  OpenAI:   ОШИБКА — {e!r}")

        print()


if __name__ == "__main__":
    asyncio.run(main())
