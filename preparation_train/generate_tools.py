import json
import os
import random
from openai import OpenAI
from dotenv import load_dotenv

# Загружаем ключи (убедись, что OPENAI_API_KEY есть в .env)
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

INPUT_FILE = "dataset_sanitized.jsonl"
OUTPUT_FILE = "data_with_tools.jsonl"
SAMPLES_TO_GENERATE = 150 # Сколько примеров обогатить тулами

# Инструкция для модели, чтобы она генерировала только JSON тулов
SYSTEM_PROMPT = """
Ты — интеллектуальный ассистент подготовки датасетов.
Твоя задача — прочитать диалог и решить, нужно ли перед последним ответом ассистента вызвать функцию.

Доступные функции:
1. `update_profile_data` — обновляет профиль. Параметры:
   - "summary": (строка) суть разговора и информация о клиенте которая записываетсая в долгосрочную память и отдается программисту (очень КРАТКО и структурировано).
   - "status": ТОЛЬКО ОДНО ИЗ "новый", "холодный", "теплый", "готов", "отказ".
2. `change_activity` — управляет статусом. Параметры:
   - "action": ТОЛЬКО ОДНО ИЗ "сон", "работа", "душ", "магазин", "прогулка", "готовка".
   - "minutes":  "На сколько минут ты уходишь. Например, для сна ставь 480-600, для душа 20-40 и тд"
    - "promise_to_return": "True - если ты хочешь написать первой после возвращения (например, 'я вышла из душа' или утреннее 'доброе утро'). False - если диалог логически завершен и писать первой не надо."

Если клиент хочет спать, желает спокойной ночи, или уже позднее время (00:00-02:00) пожелай сладких снов и уходи -> "сон". Если сейчас хорошее время для работы по контексту диалога и времени уходи работать -> "работа". 
Если идет бытовой диалог об важной информации о клиенте-> используй `update_profile_data` (пиши кратко, а не абзацами).
Если видно что статус готовности клиента изменился (например, он заинтересовался) -> `update_profile_data` с соответствующим статусом.


Если вызов нужен, верни JSON строго в таком формате:
{
  "name": "ИМЯ_ФУНКЦИИ",
  "arguments": {"ключ1": "значение1", "ключ2": "значение2"}
}
Если вызов НЕ нужен, верни просто пустой JSON: {}

Никакого лишнего текста, маркдауна или комментариев. Только JSON.
"""

def generate_tool_call(messages_history):
    # Превращаем историю в текст для анализа
    dialog_text = ""
    for msg in messages_history:
        if msg["role"] in ["user", "assistant"] and msg.get("content"):
            dialog_text += f"{msg['role'].upper()}: {msg['content']}\n"

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Оцени этот диалог и сгенерируй нужный JSON (или {{}}):\n\n{dialog_text}"}
            ],
            temperature=0.0
        )
        tool_json_str = response.choices[0].message.content.strip()
        
        # Очищаем от возможных Markdown бейджей
        if tool_json_str.startswith("```json"):
            tool_json_str = tool_json_str[7:-3].strip()
        elif tool_json_str.startswith("```"):
            tool_json_str = tool_json_str[3:-3].strip()
            
        data = json.loads(tool_json_str)
        return data if data else None
    except Exception as e:
        print(f"Ошибка генерации: {e}")
        return None

def process_dataset():
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()
    
    # Открываем файл на запись (перезаписываем, если был)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as fout:
        for i, line in enumerate(lines):
            data = json.loads(line.strip())
            messages = data.get("messages", [])
            
            if not messages:
                continue
                
            print(f"Обработка диалога {i+1}/{len(lines)}...")
            
            # Ищем индекс последней реплики ассистента
            last_assistant_idx = -1
            for idx in range(len(messages)-1, -1, -1):
                if messages[idx]["role"] == "assistant":
                    last_assistant_idx = idx
                    break
            
            # Если ассистент не отвечал в этом диалоге, просто сохраняем как есть
            if last_assistant_idx == -1:
                fout.write(line)
                continue

            # Отправляем в GPT диалог вплоть до последней реплики ассистента включительно
            tool_data = generate_tool_call(messages[:last_assistant_idx+1])
            
            # Формируем нужный текстовый тег
            if tool_data and "name" in tool_data and "arguments" in tool_data:
                func_name = tool_data["name"]
                args_str = json.dumps(tool_data["arguments"], ensure_ascii=False)
                tool_tag = f"<CALL_TOOL: {func_name}({args_str})>"
            else:
                tool_tag = "<NO_TOOL>"
            
            # Модифицируем контент последней реплики ассистента
            original_content = messages[last_assistant_idx].get("content", "")
            messages[last_assistant_idx]["content"] = f"{tool_tag}\n{original_content}"
            
            # Записываем обновленный диалог
            fout.write(json.dumps({"messages": messages}, ensure_ascii=False) + "\n")

if __name__ == "__main__":
    process_dataset()
    print(f"Готово! Все примеры обработаны и сохранены в {OUTPUT_FILE}")