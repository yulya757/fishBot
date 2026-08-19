# typo_utils.py — генерация "живых" опечаток по соседним клавишам (раскладка ЙЦУКЕН).

import random

KEYBOARD_NEIGHBORS = {
    "й": ["ц"], "ц": ["й", "у"], "у": ["ц", "к"], "к": ["у", "е"], "е": ["к", "н"],
    "н": ["е", "г"], "г": ["н", "ш"], "ш": ["г", "щ"], "щ": ["ш", "з"], "з": ["щ", "х"],
    "х": ["з", "ъ"], "ъ": ["х"],
    "ф": ["ы"], "ы": ["ф", "в"], "в": ["ы", "а"], "а": ["в", "п"], "п": ["а", "р"],
    "р": ["п", "о"], "о": ["р", "л"], "л": ["о", "д"], "д": ["л", "ж"], "ж": ["д", "э"], "э": ["ж"],
    "я": ["ч"], "ч": ["я", "с"], "с": ["ч", "м"], "м": ["с", "и"], "и": ["м", "т"],
    "т": ["и", "ь"], "ь": ["т", "б"], "б": ["ь", "ю"], "ю": ["б"],
}


def apply_keyboard_typo(text: str) -> tuple[str, str]:
    """Заменяет 1-2 буквы в тексте на соседние по клавиатуре. Возвращает (typo_text, original_text)."""
    candidate_positions = [
        i for i, ch in enumerate(text)
        if i > 0 and ch.lower() in KEYBOARD_NEIGHBORS
    ]
    if not candidate_positions:
        return text, text

    count = min(random.choice([1, 2]), len(candidate_positions))
    positions = random.sample(candidate_positions, count)

    chars = list(text)
    for pos in positions:
        ch = chars[pos]
        neighbor = random.choice(KEYBOARD_NEIGHBORS[ch.lower()])
        chars[pos] = neighbor.upper() if ch.isupper() else neighbor

    return "".join(chars), text
