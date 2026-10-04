#!/usr/bin/env python3
# sanitize_jsonl.py
import json
import re
from collections import Counter

import pymorphy3

_MORPH = pymorphy3.MorphAnalyzer()
_SINGLE_CYRILLIC_WORD_RE = re.compile(r'^[А-Яа-яЁё-]+$')

# ---------- Regex patterns ----------
EMAIL_RE = re.compile(r'(?i)\b[a-z0-9._%+-]+@(?:[a-z0-9-]+\.)+[a-z]{2,}\b')
PHONE_RE = re.compile(r'(?x)(?<!\w)(?:\+?\d{1,3}[\s\-()]*)?(?:\(?\d{2,4}\)?[\s\-()]*)?(?:\d[\s\-()]*){6,12}\d(?!\w)')
IBAN_RE = re.compile(r'\b[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}\b')
JWT_RE = re.compile(r'\b[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b')
TOKEN_PREFIX_RE = re.compile(r'(?i)\b(?:bearer|token|apikey|api[_-]?key|secret|client[_-]?secret|access[_-]?token|refresh[_-]?token)\b\s*[:=]?\s*([A-Za-z0-9._\-]{12,})')
SK_LIKE_RE = re.compile(r'\bsk-[A-Za-z0-9]{16,}\b')
LONG_HEX_RE = re.compile(r'\b[a-f0-9]{32,}\b', re.IGNORECASE)
LONG_B64_RE = re.compile(r'\b[A-Za-z0-9+/]{32,}={0,2}\b')
CODE_CONTEXT_RE = re.compile(r'(?i)\b(?:код|otp|2fa|одноразов|verification|verify|sms|смс|pin)\b[^0-9]{0,20}(\d{4,8})')
PASSWORD_KV_RE = re.compile(r'(?i)\b(?:пароль|password|pass|pwd)\b\s*[:=]\s*([^\s]{4,})')
PASSWORD_LOOSE_RE = re.compile(r'(?i)\b(?:пароль|password|pass|pwd)\b.{0,10}([^\s]{4,})')
STRONG_PASSWORD_HINT_RE = re.compile(r'(?i)\b(?:вот\s+пароль|мой\s+пароль|пароль\s+от|password\s+for|pass\s+for|логин\s+и\s+пароль)\b')

# НОВОЕ: Паттерн для банковских карт (16 цифр с пробелами или без)
CARD_RE = re.compile(r'\b(?:\d[ -]*?){13,16}\b')

# НОВОЕ: Словарь фактов для маскировки (заполни своими данными)
FACTS_TO_MASK = [
  {
    "person": "Вика",
    "age": None,
    "height": None,
    "university": None,
    "native_city": "Казань",
    "current_city": None,
    "destination_city": None,
    "districts": [],
    "masked_data": {
      "person": "<NAME>",
      "age": None,
      "height": None,
      "university": None,
      "native_city": "<NATIVE_CITY>",
      "current_city": None,
      "destination_city": None,
      "districts": []
    }
  },
  {
    "person": "Лера",
    "age": None,
    "height": None,
    "university": None,
    "native_city": None,
    "current_city": "Электросталь",
    "destination_city": "Москва",
    "districts": [],
    "masked_data": {
      "person": "<NAME>",
      "age": None,
      "height": None,
      "university": None,
      "native_city": None,
      "current_city": "<CURRENT_CITY>",
      "destination_city": "<DESTINATION_CITY>",
      "districts": []
    }
  },
  {
    "person": "Настя",
    "age": None,
    "height": "169 см",
    "university": "МФТИ",
    "native_city": None,
    "current_city": "Калининград",
    "destination_city": "Москва",
    "districts": ["Хамовники", "Сокольники"],
    "masked_data": {
      "person": "<NAME>",
      "age": None,
      "height": "<HEIGHT>",
      "university": "<UNIVERSITY>",
      "native_city": None,
      "current_city": "<CURRENT_CITY>",
      "destination_city": "<DESTINATION_CITY>",
      "districts": ["<DISTRICT>", "<DISTRICT>"]
    }
  }
]

def _all_word_forms(word: str) -> set:
    """Возвращает все словоформы (падежи/числа) одного слова через pymorphy3.
    Не-словам (числа, фразы из нескольких слов, латиница и т.д.) морфология не
    подходит — возвращают как есть, без изменений."""
    word = word.strip()
    if not word or not _SINGLE_CYRILLIC_WORD_RE.match(word):
        return {word} if word else set()
    parsed = _MORPH.parse(word)
    if not parsed:
        return {word}
    forms = {word}
    forms.update(f.word for f in parsed[0].lexeme)
    return forms


def _flatten_facts_to_mask(records):
    """Строит плоский словарь {словоформа: маска} из структурированных записей
    FACTS_TO_MASK (по одному человеку каждая, с parallel-полем masked_data).
    Каждое реальное значение раскрывается во все свои падежные формы, чтобы
    маскировались не только именительный падеж ("Казань"), но и "в Казани" и т.д."""
    flat = {}
    simple_fields = [
        "person", "age", "height", "university",
        "native_city", "current_city", "destination_city",
    ]
    for record in records:
        masked = record.get("masked_data", {})
        for field in simple_fields:
            real = record.get(field)
            mask = masked.get(field)
            if real and mask:
                for form in _all_word_forms(str(real)):
                    flat[form] = mask
        real_districts = record.get("districts") or []
        masked_districts = masked.get("districts") or []
        for real_d, mask_d in zip(real_districts, masked_districts):
            if real_d and mask_d:
                for form in _all_word_forms(str(real_d)):
                    flat[form] = mask_d
    return flat

FACTS_TO_MASK_FLAT = _flatten_facts_to_mask(FACTS_TO_MASK)

# ---------- Helpers ----------
def _safe_replace(pattern: re.Pattern, text: str, repl: str, counter: Counter, key: str) -> str:
    def _sub(m):
        counter[key] += 1
        return repl
    return pattern.sub(_sub, text)

def sanitize_text(text: str, stats: Counter) -> str:
    if not text:
        return text

    if STRONG_PASSWORD_HINT_RE.search(text):
        stats["password_line"] += 1
        return "<PASSWORD_LINE>"

    # Замена банковских карт
    text = _safe_replace(CARD_RE, text, "<CARD>", stats, "card")

    text = _safe_replace(EMAIL_RE, text, "<EMAIL>", stats, "email")
    text = _safe_replace(IBAN_RE, text, "<IBAN>", stats, "iban")

    def _code_sub(m):
        stats["code"] += 1
        return m.group(0).replace(m.group(1), "<CODE>")
    text = CODE_CONTEXT_RE.sub(_code_sub, text)

    def _pw_kv_sub(m):
        stats["password_kv"] += 1
        return m.group(0).replace(m.group(1), "<PASSWORD>")
    text = PASSWORD_KV_RE.sub(_pw_kv_sub, text)
    
    def _pw_loose_sub(m):
        stats["password_loose"] += 1
        return m.group(0).replace(m.group(1), "<PASSWORD>")
    text = PASSWORD_LOOSE_RE.sub(_pw_loose_sub, text)

    text = _safe_replace(JWT_RE, text, "<TOKEN>", stats, "jwt")
    text = _safe_replace(SK_LIKE_RE, text, "<TOKEN>", stats, "sk_like")

    def _token_prefix_sub(m):
        stats["token_prefix"] += 1
        return m.group(0).replace(m.group(1), "<TOKEN>")
    text = TOKEN_PREFIX_RE.sub(_token_prefix_sub, text)

    text = _safe_replace(LONG_HEX_RE, text, "<TOKEN>", stats, "long_hex")
    text = _safe_replace(LONG_B64_RE, text, "<TOKEN>", stats, "long_b64")
    text = _safe_replace(PHONE_RE, text, "<PHONE>", stats, "phone")

    # Маскировка личных фактов (имена, города, вуз, район и т.д.)
    for word, mask in FACTS_TO_MASK_FLAT.items():
        text, n = re.subn(rf'\b{re.escape(word)}\b', mask, text, flags=re.IGNORECASE)
        if n:
            stats["fact"] += n

    return text

def sanitize_jsonl(in_path: str, out_path: str) -> Counter:
    stats = Counter()
    bad_lines = 0

    with open(in_path, "r", encoding="utf-8") as fin, open(out_path, "w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                bad_lines += 1
                continue

            msgs = obj.get("messages", [])
            if isinstance(msgs, list):
                for msg in msgs:
                    if isinstance(msg, dict) and "content" in msg and isinstance(msg["content"], str):
                        msg["content"] = sanitize_text(msg["content"], stats)

            fout.write(json.dumps(obj, ensure_ascii=False) + "\n")

    stats["bad_lines"] = bad_lines
    return stats

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--in", dest="inp", default="dataset1.jsonl", help="Input JSONL")
    p.add_argument("--out", dest="out", default="dataset_sanitized1.jsonl", help="Output JSONL")
    args = p.parse_args()

    stats = sanitize_jsonl(args.inp, args.out)
    print("Done. Stats:")
    for k, v in stats.most_common():
        print(f"  {k}: {v}")