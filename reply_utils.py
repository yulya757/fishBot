# reply_utils.py — обработка сырых ответов LoRA-модели.
# Не тянет unsloth/torch, поэтому импортируется и тестируется без GPU-окружения.

import re

THINK_RE = re.compile(r"<think>.*?</think>", re.S)


def clean_reply(text):
    """Убирает блоки размышлений, включая незакрытые (обрезанные лимитом токенов),
    чтобы они не попадали в историю диалога и не портили следующие ответы."""
    text = THINK_RE.sub("", text)
    if "</think>" in text:  # блок был открыт ещё в промпте
        text = text.split("</think>")[-1]
    if "<think>" in text:  # блок открыт, но не закрыт — дальше только размышления
        text = text.split("<think>")[0]
    return text.strip()


def merge_roles(history):
    """Склеивает подряд идущие сообщения одной роли через \n —
    чат-темплейт Qwen требует строгого чередования ролей."""
    merged = []
    for m in history:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] += "\n" + m["content"]
        else:
            merged.append({"role": m["role"], "content": m["content"]})
    return merged
