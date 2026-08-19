import json
import os
import asyncio
import socket
import re
import subprocess
import time
import traceback
from urllib.parse import urlparse
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.enums import ChatAction
import database
from ai_backends import ai_client, TOOLS, generate_reply, INFERENCE_BACKEND, LOCAL_MODEL_URL
from typo_utils import apply_keyboard_typo
from datetime import datetime, timedelta
import random

# Загружаем ключи из .env
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID'))

# Кеш для разрешенных чатов
ALLOWED_CHATS_CACHE = set() 
USER_BUSY_UNTIL = {}           # {chat_id: datetime} - до скольки бот занят
USER_PENDING_RETURN = {}       # {chat_id: "activity_name"} - откуда бот должен вернуться
LAST_OUR_MESSAGE_TIME = {}     # {chat_id: datetime} - когда мы писали в последний раз
LAST_OUR_MESSAGE_ID = {}       # {chat_id: int} - id последнего отправленного нами сообщения
LAST_USER_MESSAGE_TIME = {}    # {chat_id: datetime} - когда юзер писал в последний раз

# Плановые "живые" опечатки с самоисправлением
PENDING_TYPO_CHATS = set()                        # чаты, которым на следующей отправке нужно вставить опечатку
TYPO_DAILY_STATE = {"date": None, "quota": 0, "used": 0}
LAST_TYPO_EVENT_AT = None                         # datetime последнего события, для антикластеризации
TYPO_ACTIVE_WINDOW_MIN = 20   # сообщения должны идти плотно в пределах этого окна
TYPO_MIN_STREAK = 7           # минимум сообщений подряд для "активного диалога"
TYPO_COOLDOWN_MIN = 90        # не чаще одного события за это время

# Админ-панель на инлайн-кнопках
ADMIN_STATE = {"waiting_username": False, "waiting_chat_id": False}   # ждем ли текстовый ввод (поиск / ручной chat_id)
CONTACTS_PAGE_SIZE = 8
ADMIN_ADD_SELECTION = set()                 # chat_id'ы, отмеченные в мультивыборе "Добавить чат"
ADMIN_LOG_VIEW = {"chat_id": None, "message_ids": []}  # временные сообщения лога — удаляются при закрытии

# Инициализируем бота и диспетчер (aiogram)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# Загрузка системного промпта
try:
    with open("system_promt.txt", "r", encoding="utf-8") as f:
        SYSTEM_PROMPT = f.read().strip()
except FileNotFoundError:
    print("Ошибка: Файл system_promt.txt не найден.")
    SYSTEM_PROMPT = ""
except Exception as e:
    print(f"Ошибка при чтении system_promt.txt: {e}")
    SYSTEM_PROMPT = ""

# Структура буфера для умного накопления сообщений в бизнес-режиме
USER_MESSAGE_BUFFERS = {}
USER_MESSAGE_TASKS = {}
DEBOUNCE_TIME = 2.0
MAX_WAIT_TIME = 7.0

# Автообновление сводки/статуса каждые N сообщений в диалоге (независимо от ручного /summarize)
SUMMARY_EVERY_N_MESSAGES = 15
MESSAGE_COUNT_SINCE_SUMMARY = {}  # {chat_id: int} - сколько сообщений накопилось с последней сводки


def track_message_for_summary(chat_id: int):
    """Считает сообщения в диалоге с момента последней сводки; при достижении порога
    запускает generate_and_save_summary в фоне и сбрасывает счетчик."""
    count = MESSAGE_COUNT_SINCE_SUMMARY.get(chat_id, 0) + 1
    if count >= SUMMARY_EVERY_N_MESSAGES:
        MESSAGE_COUNT_SINCE_SUMMARY[chat_id] = 0
        print(f"[SUMMARY_AUTO] {chat_id}: набралось {count} сообщений — запускаю автообновление сводки")
        asyncio.create_task(generate_and_save_summary(chat_id))
    else:
        MESSAGE_COUNT_SINCE_SUMMARY[chat_id] = count

def admin_close_keyboard() -> types.InlineKeyboardMarkup:
    """Кнопка для закрытия (удаления) одноразовых служебных сообщений админу."""
    return types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="✖️ Закрыть", callback_data="admclose")
    ]])


async def send_admin_message(message_text: str, closable: bool = True):
    """Отправляет сообщение администратору напрямую в бота.
    closable=True вешает кнопку '✖️ Закрыть' (для одноразовых уведомлений об ошибках/статусах).
    Возвращает отправленное сообщение (или None при сбое), чтобы вызывающий код мог позже
    отредактировать его в финальный результат через finish_admin_status()."""
    try:
        return await bot.send_message(
            ADMIN_ID, message_text, reply_markup=admin_close_keyboard() if closable else None
        )
    except Exception as e:
        print(f"Ошибка при отправке сообщения администратору: {e}")
        return None


async def finish_admin_status(status_msg, text: str):
    """Редактирует стартовое статусное сообщение ('Начинаю...') в финальный результат
    с кнопкой закрытия — вместо отдельного 'висящего' сообщения, которое нечем убрать."""
    if status_msg is None:
        await send_admin_message(text)
        return
    await safe_edit_text(status_msg, text, reply_markup=admin_close_keyboard())

async def update_allowed_chats_cache():
    """Обновляет кеш разрешенных чатов из базы данных."""
    global ALLOWED_CHATS_CACHE
    allowed_chats_data = database.get_all_allowed_chats()
    ALLOWED_CHATS_CACHE = {chat[0] for chat in allowed_chats_data}
    print(f"Кеш разрешенных чатов обновлен: {ALLOWED_CHATS_CACHE}")
    await send_admin_message(f"Кеш разрешенных чатов обновлен: {ALLOWED_CHATS_CACHE}")

async def trigger_proactive_ai(chat_id: int, context_prompt: str):
    """Вызывает ИИ для инициативного сообщения и отправляет его от лица аккаунта."""
    
    # 1. Достаем ключ-пропуск из базы
    biz_conn_id = database.get_biz_conn_id(chat_id)
    if not biz_conn_id:
        print(f"Не могу написать в {chat_id}: нет business_connection_id в базе.")
        await send_admin_message(f"Не могу написать в {chat_id}: нет business_connection_id в базе.")
        return

    # 2. Формируем запрос
    dynamic_extra = context_prompt
    recent_msgs = database.get_recent_messages(chat_id, limit=8)
    history_pairs = [
        {"role": "assistant" if msg[0] == "me" else "user", "content": msg[1]}
        for msg in recent_msgs
    ]
    messages_for_ai = [{"role": "system", "content": SYSTEM_PROMPT + "\n\n" + dynamic_extra}] + history_pairs

    try:
        # 3. Делаем запрос к активному бэкенду (OpenAI или локальная модель)
        ai_reply, _tool_calls, _tool_decision_failed = await generate_reply(
            messages_for_ai, dynamic_extra, history_pairs
        )

        # 4. Сохраняем в БД и отправляем
        if ai_reply:
            database.save_message(chat_id, "me", ai_reply)
            # Отправляем с нашим ключом!
            await split_and_send_messages(chat_id, ai_reply, biz_conn_id) 
            
            # Обновляем таймер
            global LAST_OUR_MESSAGE_TIME
            LAST_OUR_MESSAGE_TIME[chat_id] = datetime.now()
            
    except Exception as e:
        print(f"Ошибка проактивного ИИ: {e}")

async def generate_and_save_summary(chat_id: int):
    """Генерирует сводку последних сообщений и определяет статус для чата через OpenAI."""
    print(f"Начинаю генерацию сводки и статуса для чата {chat_id}...")
    status_msg = await send_admin_message(f"Начинаю генерацию сводки и статуса для чата {chat_id}...", closable=False)

    recent_msgs = database.get_recent_messages(chat_id, limit=20)

    if not recent_msgs:
        print(f"В чате {chat_id} нет сообщений для сводки.")
        await finish_admin_status(status_msg, f"В чате {chat_id} нет сообщений для сводки.")
        return

    try:
        with open("summary_prompt.txt", "r", encoding="utf-8") as f:
            summary_and_status_prompt = f.read().strip()
    except FileNotFoundError:
        print("Файл summary_prompt.txt не найден.")
        summary_and_status_prompt = "Сделай сводку."
    except Exception as e:
        print(f"Ошибка при чтении summary_prompt.txt: {e}")
        summary_and_status_prompt = "Сделай сводку."

    # Промпт требует Delta Updates относительно "старого профиля" — без этого модели
    # не из чего делать дельту, и предыдущий контекст (долгосрочная память) терялся бы
    # при каждой перегенерации сводки.
    previous_profile = database.get_profile_fields(chat_id)
    system_content = summary_and_status_prompt
    if previous_profile and previous_profile.get("summary"):
        system_content += (
            "\n\n[Текущий сохраненный профиль клиента (JSON, обнови дельтой, не теряя старые факты): "
            + json.dumps(previous_profile, ensure_ascii=False) + "]"
        )

    messages_for_ai = [{"role": "system", "content": system_content}]
    for msg in recent_msgs:
        role = "assistant" if msg[0] == "me" else "user"
        messages_for_ai.append({"role": role, "content": msg[1]})

    try:
        response = await ai_client.chat.completions.create(
            model="gpt-4o",
            messages=messages_for_ai,
            tools=TOOLS,
            tool_choice={"type": "function", "function": {"name": "update_profile_data"}}
        )
        
        response_message = response.choices[0].message
        tool_calls = response_message.tool_calls

        if tool_calls and tool_calls[0].function.name == "update_profile_data":
            function_args = json.loads(tool_calls[0].function.arguments)
            summary_text = function_args.get("summary")
            status_text = function_args.get("status")

            if summary_text and status_text:
                old_status = database.get_profile_status(chat_id)
                database.update_profile_summary(chat_id, summary_text)
                database.update_profile_status(chat_id, status_text)
                database.update_profile_fields(
                    chat_id,
                    name=function_args.get("name"),
                    age=function_args.get("age"),
                    location=function_args.get("location"),
                    occupation=function_args.get("occupation"),
                    financial_status=function_args.get("financial_status"),
                    pain_points=function_args.get("pain_points") or [],
                    personal_facts=function_args.get("personal_facts") or [],
                    next_suggested_topic=function_args.get("next_suggested_topic"),
                )
                database.log_activity(chat_id, f"Сводка обновлена: {summary_text}")
                print(f"Для чата {chat_id} обновлены сводка: \"{summary_text}\", и статус: \"{status_text}\".")
                await finish_admin_status(
                    status_msg, f"Для чата {chat_id} обновлены сводка: \"{summary_text}\", и статус: \"{status_text}\"."
                )

                if status_text != old_status:
                    database.log_activity(chat_id, f"Статус изменен: {old_status} → {status_text}")
                    await notify_status_change(chat_id, old_status, status_text)
            else:
                await finish_admin_status(status_msg, "ИИ не вернул сводку/статус.")
        else:
            print(f"ИИ не использовал функцию для обновления профиля.")
            await finish_admin_status(status_msg, "ИИ не использовал функцию для обновления профиля.")

    except Exception as e:
        print(f"Ошибка при генерации сводки для чата {chat_id}: {e}")
        await finish_admin_status(status_msg, f"Ошибка при генерации сводки для чата {chat_id}: {e}")

async def get_ai_response(chat_id: int):
    """Формирует контекст и запрашивает ответ у активного бэкенда (OpenAI или локальная модель)."""
    if not SYSTEM_PROMPT:
        print("[AI] !!! ПРЕДУПРЕЖДЕНИЕ: SYSTEM_PROMPT пустой — system_promt.txt не загрузился при старте")

    # Узнаем текущее время
    current_time = datetime.now().strftime("%H:%M")
    dynamic_extra = f"[СИСТЕМНОЕ ВРЕМЯ СЕЙЧАС: {current_time}. Если сейчас от 00:00 до 02:00, и диалог логически завершен, попрощайся (пожелай спокойной ночи) и ВЫЗОВИ функцию change_activity на 480-600 минут. Если ты идешь работать или в душ, тоже вызови эту функцию.]"

    # Долгосрочная память: не только текстовая сводка, но и структурированные факты/боли,
    # чтобы бот реально их использовал в диалоге (иначе они просто лежат в БД без пользы)
    profile = database.get_profile_fields(chat_id)
    if profile and profile.get("summary"):
        memory_lines = [f"Сводка: {profile['summary']}"]
        if profile.get("name"):
            memory_lines.append(f"Имя: {profile['name']}")
        if profile.get("age"):
            memory_lines.append(f"Возраст: {profile['age']}")
        if profile.get("location"):
            memory_lines.append(f"Локация: {profile['location']}")
        if profile.get("occupation"):
            memory_lines.append(f"Род занятий: {profile['occupation']}")
        if profile.get("financial_status"):
            memory_lines.append(f"Финансовое положение: {profile['financial_status']}")
        if profile.get("personal_facts"):
            memory_lines.append(f"Бытовые факты: {', '.join(profile['personal_facts'])}")
        if profile.get("pain_points"):
            memory_lines.append(f"Боли/триггеры: {', '.join(profile['pain_points'])}")
        if profile.get("next_suggested_topic"):
            memory_lines.append(f"Рекомендуемая тема: {profile['next_suggested_topic']}")
        dynamic_extra += "\n\n[Долгосрочная память о клиенте:\n" + "\n".join(memory_lines) + "]"

    messages_for_ai = [{"role": "system", "content": SYSTEM_PROMPT + "\n\n" + dynamic_extra}]

    recent_msgs = database.get_recent_messages(chat_id, limit=8)
    history_pairs = [
        {"role": "assistant" if msg[0] == "me" else "user", "content": msg[1]}
        for msg in recent_msgs
    ]
    messages_for_ai.extend(history_pairs)
    print(f"[AI] {chat_id}: отправляю запрос ({len(history_pairs)} сообщ. в истории)...")

    try:
        content, tool_calls, tool_decision_failed = await generate_reply(
            messages_for_ai, dynamic_extra, history_pairs
        )
    except Exception as e:
        print(f"[AI] !!! ИСКЛЮЧЕНИЕ при получении ответа ИИ для чата {chat_id}:")
        traceback.print_exc()
        await send_admin_message(f"Ошибка при получении ответа ИИ для чата {chat_id}: {e}")
        return None, None

    print(f"[AI] {chat_id}: получен ответ, tool_decision_failed={tool_decision_failed}")
    if tool_decision_failed:
        await send_admin_message(
            f"Для чата {chat_id}: статус/активность не обновились (сбой decide_tools), но ответ отправлен."
        )

    return content, tool_calls


async def split_and_send_messages(chat_id: int, text: str, biz_conn_id: str, reply_to_msg_id: int = None):
    """Разделяет текст и отправляет от лица бизнес-аккаунта с имитацией набора текста."""
    # ИИ (особенно локальный бэкенд) иногда отдает литеральный "\n" (два символа: слэш и n)
    # вместо настоящего переноса строки — нормализуем перед разбивкой
    text = text.replace('\\n', '\n')
    parts = [p.strip() for p in re.split(r'\n\s*\n|\n', text) if p.strip()] # разбивка на одинарный или двойной перенос строки
    if not parts or (len(parts) == 1 and len(parts[0]) < 50):
        parts = [text]
    print(f"[SEND] {chat_id}: отправляю {len(parts)} сообщени(е/й), biz_conn_id={biz_conn_id!r}")

    # Плановая "живая" опечатка: событие списывается сразу, чтобы не сработать повторно
    do_typo = chat_id in PENDING_TYPO_CHATS
    PENDING_TYPO_CHATS.discard(chat_id)
    typo_part_index = None
    if do_typo:
        for i, part in enumerate(parts):
            if len(part) >= 12:
                typo_part_index = i
                break

    for i, part in enumerate(parts):
        if not part:
            continue

        # Передаем business_connection_id, чтобы печатать от имени личного аккаунта
        await bot.send_chat_action(chat_id, ChatAction.TYPING, business_connection_id=biz_conn_id)
        await asyncio.sleep(len(part) * 0.1) # Задержка, зависящая от длины текста

        send_text = part
        if i == typo_part_index:
            typo_text, original_text = apply_keyboard_typo(part)
            if typo_text != original_text:
                send_text = typo_text

        if i == 0 and reply_to_msg_id:
            sent_msg = await bot.send_message(chat_id, send_text, business_connection_id=biz_conn_id, reply_to_message_id=reply_to_msg_id)
        else:
            sent_msg = await bot.send_message(chat_id, send_text, business_connection_id=biz_conn_id)
        print(f"[SEND] {chat_id}: часть {i + 1}/{len(parts)} отправлена (msg_id={sent_msg.message_id}): {send_text!r}")

        LAST_OUR_MESSAGE_ID[chat_id] = sent_msg.message_id

        if i == typo_part_index and send_text != part:
            await asyncio.sleep(random.uniform(1.5, 4.0))
            await bot.edit_message_text(
                text=part, chat_id=chat_id, message_id=sent_msg.message_id, business_connection_id=biz_conn_id
            )

async def process_user_message_buffer(chat_id: int):
    global USER_MESSAGE_BUFFERS, USER_MESSAGE_TASKS, USER_BUSY_UNTIL, LAST_OUR_MESSAGE_TIME

    if chat_id not in USER_MESSAGE_BUFFERS or not USER_MESSAGE_BUFFERS[chat_id]["parts"]:
        print(f"[BUFFER] {chat_id}: буфер пуст на момент обработки — нечего отправлять")
        return

    buffer_data = USER_MESSAGE_BUFFERS.pop(chat_id)
    parts = buffer_data["parts"]
    user_full_text = " ".join(p["text"] for p in parts).strip()
    biz_conn_id = buffer_data["business_connection_id"]
    original_message_id = parts[0]["message_id"]
    print(f"[BUFFER] {chat_id}: начинаю обработку {len(parts)} куск(ов): {user_full_text!r}")

    now = datetime.now()

    # 1. ПРОВЕРКА НА СОН/ЗАНЯТОСТЬ
    if chat_id in USER_BUSY_UNTIL and now < USER_BUSY_UNTIL[chat_id]:
        print(f"[BUFFER] {chat_id}: девушка занята до {USER_BUSY_UNTIL[chat_id]}. Сообщение сохранено, но ответа пока не будет.")
        for part in parts:
            database.save_message(chat_id, "user", part["text"], tg_message_id=part["message_id"])
            track_message_for_summary(chat_id)
        return # Просто сохраняем в БД и прерываем обработку!

    # 2. ДИНАМИЧЕСКИЕ ПАУЗЫ (Быстро в диалоге, медленно после молчания)
    last_msg_time = LAST_OUR_MESSAGE_TIME.get(chat_id)
    if last_msg_time:
        time_since_last = (now - last_msg_time).total_seconds()
        if time_since_last > 600: # Если молчали больше 10 минут
            # Имитируем, что телефон не в руках. Задержка от 3 до 8 минут
            delay = random.randint(180, 480)
            print(f"[BUFFER] {chat_id}: диалог возобновлен после {int(time_since_last)} сек молчания. Ждем {delay} сек перед ответом...")
            await asyncio.sleep(delay)

    # Сохраняем каждый кусочек отдельной строкой (точная привязка к tg_message_id) и получаем ответ ИИ
    for part in parts:
        database.save_message(chat_id, "user", part["text"], tg_message_id=part["message_id"])
        track_message_for_summary(chat_id)
    print(f"[BUFFER] {chat_id}: сообщения сохранены в БД, запрашиваю ответ ИИ (backend={INFERENCE_BACKEND})...")
    await bot.send_chat_action(chat_id, ChatAction.TYPING, business_connection_id=biz_conn_id)

    # Здесь нужно немного изменить get_ai_response, чтобы он мог возвращать вызовы функций!
    ai_reply, tool_calls = await get_ai_response(chat_id)
    print(f"[BUFFER] {chat_id}: ИИ вернул ai_reply={ai_reply!r}, tool_calls={[t.function.name for t in tool_calls] if tool_calls else None}")

    # 3. ОБРАБОТКА РЕШЕНИЯ ИИ ПОЙТИ СПАТЬ/ПО ДЕЛАМ
    if tool_calls:
        for tool in tool_calls:
            if tool.function.name == "change_activity":
                args = json.loads(tool.function.arguments)
                busy_minutes = args.get("minutes", 0)
                activity = args.get("activity", "дела")

                USER_BUSY_UNTIL[chat_id] = datetime.now() + timedelta(minutes=busy_minutes)
                print(f"[BUFFER] {chat_id}: ИИ решил уйти в статус: {activity} на {busy_minutes} минут.")
                database.log_activity(chat_id, f"Активность: {activity} на {busy_minutes} мин (до {USER_BUSY_UNTIL[chat_id].strftime('%H:%M')})")

                # Если ИИ ничего не написал текстом, а просто ушел спать
                if not ai_reply:
                    print(f"[BUFFER] {chat_id}: текста нет, только смена активности — на этом всё")
                    return

    if ai_reply:
        database.save_message(chat_id, "me", ai_reply)
        track_message_for_summary(chat_id)
        print(f"[BUFFER] {chat_id}: отправляю ответ клиенту...")
        await split_and_send_messages(chat_id, ai_reply, biz_conn_id, reply_to_msg_id=original_message_id)
        LAST_OUR_MESSAGE_TIME[chat_id] = datetime.now()
        print(f"[BUFFER] {chat_id}: ответ отправлен")
    else:
        print(f"[BUFFER] {chat_id}: ИИ не вернул текст ответа (ai_reply пуст) — клиенту ничего не отправлено")


async def safe_edit_text(message: types.Message, text: str, reply_markup=None):
    """edit_text, который не падает, если текст/клавиатура не изменились (Telegram отвечает 400
    'message is not modified' — например, при повторном нажатии '🔄 Обновить' без изменений)."""
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


def format_contact_label(chat_id: int, username: str, first_name: str) -> str:
    """Человекочитаемая подпись для контакта: 'Имя (@username)' с фолбэком на chat_id."""
    name = first_name or str(chat_id)
    return f"{name} (@{username})" if username else name


def build_admin_panel_keyboard() -> types.ReplyKeyboardMarkup:
    """Постоянная нижняя клавиатура для админа: всего два пункта."""
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="➕ Добавить чат"), types.KeyboardButton(text="💬 Чаты")],
        ],
        resize_keyboard=True,
    )


async def render_add_picker(page: int):
    """Мультивыбор известных, но еще не разрешенных чатов (кнопка '➕ Добавить чат')."""
    offset = page * CONTACTS_PAGE_SIZE
    rows = database.get_unallowed_contacts(limit=CONTACTS_PAGE_SIZE, offset=offset)
    total = database.get_unallowed_contacts_count()

    buttons = []
    for chat_id, username, first_name in rows:
        mark = "☑️" if chat_id in ADMIN_ADD_SELECTION else "⬜"
        label = format_contact_label(chat_id, username, first_name)
        buttons.append([types.InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"addpick:{chat_id}:{page}")])

    nav_row = []
    if page > 0:
        nav_row.append(types.InlineKeyboardButton(text="« Пред", callback_data=f"addlist:{page - 1}"))
    if (page + 1) * CONTACTS_PAGE_SIZE < total:
        nav_row.append(types.InlineKeyboardButton(text="След »", callback_data=f"addlist:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    if ADMIN_ADD_SELECTION:
        buttons.append([types.InlineKeyboardButton(
            text=f"✅ Добавить выбранные ({len(ADMIN_ADD_SELECTION)})", callback_data="addconfirm"
        )])
    buttons.append([types.InlineKeyboardButton(text="🔢 Ввести chat_id вручную", callback_data="addbyid")])
    buttons.append([types.InlineKeyboardButton(text="🚫 Отмена", callback_data="addcancel")])

    text = "➕ Выбери чат(ы) для добавления (тапни, чтобы отметить):"
    if not rows and not ADMIN_ADD_SELECTION:
        text += "\n\nПока никто новый не писал."

    return text, types.InlineKeyboardMarkup(inline_keyboard=buttons)


async def render_chats_picker(page: int, search: str = None):
    """Список уже разрешенных чатов (кнопка '💬 Чаты'), выбор одного открывает карточку чата."""
    offset = page * CONTACTS_PAGE_SIZE
    rows = database.get_allowed_contacts(limit=CONTACTS_PAGE_SIZE, offset=offset, search=search)
    total = database.get_allowed_contacts_count(search=search)

    buttons = [
        [types.InlineKeyboardButton(
            text=format_contact_label(chat_id, username, first_name), callback_data=f"detail:{chat_id}"
        )]
        for chat_id, username, first_name in rows
    ]

    nav_row = []
    if not search:
        if page > 0:
            nav_row.append(types.InlineKeyboardButton(text="« Пред", callback_data=f"chatlist:{page - 1}"))
        if (page + 1) * CONTACTS_PAGE_SIZE < total:
            nav_row.append(types.InlineKeyboardButton(text="След »", callback_data=f"chatlist:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)
    buttons.append([types.InlineKeyboardButton(text="🔎 Найти по юзернейму", callback_data="chatsearch")])

    text = f"💬 Результаты поиска «{search}»:" if search else "💬 Разрешенные чаты:"
    if not rows:
        text += "\n\nНичего не найдено." if search else "\n\nСписок пуст."

    return text, types.InlineKeyboardMarkup(inline_keyboard=buttons)


async def render_chat_detail(chat_id: int):
    """Карточка конкретного чата: статус, занятость, сводка и все действия одной кнопкой."""
    contact = database.get_known_contact(chat_id)
    username = contact[1] if contact else None
    first_name = contact[2] if contact else None
    label = format_contact_label(chat_id, username, first_name)

    status = database.get_profile_status(chat_id)
    summary = database.get_profile_summary(chat_id) or "—"

    busy_until = USER_BUSY_UNTIL.get(chat_id)
    if busy_until and datetime.now() < busy_until:
        busy_line = f"🔴 Занята до {busy_until.strftime('%H:%M')}"
    else:
        busy_line = "🟢 Свободна"

    text = (
        f"{label}\nchat_id: {chat_id}\n\n"
        f"Статус: {status}\n{busy_line}\n\n"
        f"Сводка: {summary}"
    )

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="📊 Обновить сводку", callback_data=f"detailsumm:{chat_id}")],
        [
            types.InlineKeyboardButton(text="🔤 Тест опечатки", callback_data=f"detailtypo:{chat_id}"),
            types.InlineKeyboardButton(text="🔄 Обновить", callback_data=f"detail:{chat_id}"),
        ],
        [types.InlineKeyboardButton(text="📜 Лог действий", callback_data=f"detaillog:{chat_id}")],
        [types.InlineKeyboardButton(text="🗑 Удалить из разрешенных", callback_data=f"detaildel:{chat_id}")],
        [types.InlineKeyboardButton(text="« Назад к чатам", callback_data="chatlist:0")],
    ])
    return text, keyboard


async def notify_status_change(chat_id: int, old_status: str, new_status: str):
    """Пингует админа, когда статус клиента меняется, с возможностью оставить или откатить."""
    contact = database.get_known_contact(chat_id)
    label = format_contact_label(chat_id, contact[1] if contact else None, contact[2] if contact else None)
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="✅ Оставить", callback_data=f"statuskeep:{chat_id}"),
        types.InlineKeyboardButton(text=f"↩️ Вернуть «{old_status}»", callback_data=f"statusrevert:{chat_id}:{old_status}"),
    ]])
    await bot.send_message(
        ADMIN_ID, f"Статус чата {label} изменился: {old_status} → {new_status}", reply_markup=keyboard
    )


# --- ОБРАБОТЧИКИ КОМАНД АДМИНА (В ЛИЧКУ БОТУ) ---
# Эти команды ты отправляешь в личные сообщения самого бота

@dp.message(Command("addchat"))
async def add_chat_command(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
    chat_id_str = command.args
    if not chat_id_str:
        await message.answer("Используй `/addchat <ID_ЧАТА>`.")
        return
    target_chat_id = int(chat_id_str)
    database.add_allowed_chat(target_chat_id)
    await update_allowed_chats_cache() 
    await message.answer(f"Чат {target_chat_id} успешно добавлен!")

@dp.message(Command("removechat"))
async def remove_chat_command(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
    chat_id_str = command.args
    if not chat_id_str:
        await message.answer("Используй `/removechat <ID_ЧАТА>`.")
        return
    target_chat_id = int(chat_id_str)
    database.remove_allowed_chat(target_chat_id)
    await update_allowed_chats_cache() 
    await message.answer(f"Чат {target_chat_id} удален!")

@dp.message(Command("listchats"))
async def list_chats_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    allowed_chats_data = database.get_all_allowed_chats()
    if not allowed_chats_data:
        await message.answer("Список пуст.")
        return
    response_msg = "Разрешенные чаты:\n" + "\n".join([f"- `{c[0]}`" for c in allowed_chats_data])
    await message.answer(response_msg)

@dp.message(Command("summarize"))
async def summarize_command(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
    chat_id_str = command.args
    if not chat_id_str:
        await message.answer("Укажи ID чата: `/summarize <ID_ЧАТА>`")
        return
    await message.answer(f"Начинаю суммировать чат {chat_id_str}...")
    await generate_and_save_summary(int(chat_id_str))
    await message.answer("Сводка обновлена!")

@dp.message(Command("forcetypo"))
async def force_typo_command(message: types.Message, command: CommandObject):
    """Ручной тест механизма опечаток: ставит чат в очередь без ожидания реальных условий."""
    if message.from_user.id != ADMIN_ID:
        return
    chat_id_str = command.args
    if not chat_id_str:
        await message.answer("Используй `/forcetypo <ID_ЧАТА>`.")
        return
    target_chat_id = int(chat_id_str)
    PENDING_TYPO_CHATS.add(target_chat_id)
    await message.answer(f"Опечатка запланирована для чата {target_chat_id} на следующую отправку.")


# --- АДМИН-ПАНЕЛЬ НА ИНЛАЙН-КНОПКАХ ---

@dp.message(Command("panel"))
async def panel_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Панель готова, кнопки внизу экрана.", reply_markup=build_admin_panel_keyboard())


@dp.message(F.text == "➕ Добавить чат")
async def panel_add_button(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    ADMIN_STATE["waiting_username"] = False
    ADMIN_STATE["waiting_chat_id"] = False
    ADMIN_ADD_SELECTION.clear()
    text, kb = await render_add_picker(0)
    await message.answer(text, reply_markup=kb)


@dp.message(F.text == "💬 Чаты")
async def panel_chats_button(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    ADMIN_STATE["waiting_username"] = False
    ADMIN_STATE["waiting_chat_id"] = False
    text, kb = await render_chats_picker(0)
    await message.answer(text, reply_markup=kb)


@dp.callback_query(F.data == "admclose")
async def cb_admin_close(callback: types.CallbackQuery):
    """Универсальная кнопка закрытия одноразовых служебных сообщений админу."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


# --- Мультивыбор "Добавить чат" ---

@dp.callback_query(F.data.startswith("addlist:"))
async def cb_add_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    page = int(callback.data.split(":")[1])
    text, kb = await render_add_picker(page)
    await safe_edit_text(callback.message,text, reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("addpick:"))
async def cb_add_pick(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    _, chat_id_str, page_str = callback.data.split(":")
    chat_id = int(chat_id_str)
    if chat_id in ADMIN_ADD_SELECTION:
        ADMIN_ADD_SELECTION.discard(chat_id)
    else:
        ADMIN_ADD_SELECTION.add(chat_id)
    text, kb = await render_add_picker(int(page_str))
    await safe_edit_text(callback.message,text, reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "addconfirm")
async def cb_add_confirm(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    added = list(ADMIN_ADD_SELECTION)
    for chat_id in added:
        database.add_allowed_chat(chat_id)
        database.log_activity(chat_id, "Добавлен в разрешенные администратором (панель)")
    ADMIN_ADD_SELECTION.clear()
    await update_allowed_chats_cache()
    await safe_edit_text(callback.message, f"✅ Добавлено чатов: {len(added)}.", reply_markup=admin_close_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "addcancel")
async def cb_add_cancel(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    ADMIN_ADD_SELECTION.clear()
    await safe_edit_text(callback.message, "Отменено.", reply_markup=admin_close_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "addbyid")
async def cb_add_by_id(callback: types.CallbackQuery):
    """Позволяет добавить чат напрямую по chat_id, минуя выбор из адресной книги —
    полезно, когда человек еще ни разу не писал и его нет среди известных контактов."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    ADMIN_STATE["waiting_username"] = False
    ADMIN_STATE["waiting_chat_id"] = True
    await callback.message.answer("Пришли chat_id числом.")
    await callback.answer()


# --- Список разрешенных чатов и поиск ---

@dp.callback_query(F.data.startswith("chatlist:"))
async def cb_chat_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    page = int(callback.data.split(":")[1])
    text, kb = await render_chats_picker(page)
    await safe_edit_text(callback.message,text, reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "chatsearch")
async def cb_chat_search(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    ADMIN_STATE["waiting_chat_id"] = False
    ADMIN_STATE["waiting_username"] = True
    await callback.message.answer("Введи юзернейм (с @ или без) для поиска среди разрешенных чатов.")
    await callback.answer()


# --- Карточка чата ---

@dp.callback_query(F.data.startswith("detail:"))
async def cb_detail(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    chat_id = int(callback.data.split(":")[1])
    text, kb = await render_chat_detail(chat_id)
    await safe_edit_text(callback.message,text, reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("detailsumm:"))
async def cb_detail_summary(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    chat_id = int(callback.data.split(":")[1])
    await callback.answer("Генерирую сводку...")
    await generate_and_save_summary(chat_id)
    text, kb = await render_chat_detail(chat_id)
    await safe_edit_text(callback.message,text, reply_markup=kb)


@dp.callback_query(F.data.startswith("detailtypo:"))
async def cb_detail_typo(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    chat_id = int(callback.data.split(":")[1])
    PENDING_TYPO_CHATS.add(chat_id)
    await callback.answer("Опечатка запланирована на следующую отправку.")


@dp.callback_query(F.data.startswith("detaildel:"))
async def cb_detail_delete(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    chat_id = int(callback.data.split(":")[1])
    database.remove_allowed_chat(chat_id)
    database.log_activity(chat_id, "Удален из разрешенных администратором (панель)")
    await update_allowed_chats_cache()
    text, kb = await render_chats_picker(0)
    await safe_edit_text(callback.message,text, reply_markup=kb)
    await callback.answer("Удалено")


@dp.callback_query(F.data.startswith("detaillog:"))
async def cb_detail_log(callback: types.CallbackQuery):
    """Присылает лог действий отдельными сообщениями, которые полностью удаляются при закрытии."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    chat_id = int(callback.data.split(":")[1])
    entries = database.get_activity_log(chat_id, limit=20)
    lines_text = "\n".join(f"[{ts}] {event}" for event, ts in entries) if entries else "Лог пуст."
    full_text = f"📜 Лог действий по чату {chat_id}:\n\n{lines_text}"

    # На случай длинного лога режем по лимиту Telegram, храним id всех чанков для последующего удаления
    chunks = [full_text[i:i + 3500] for i in range(0, len(full_text), 3500)] or [full_text]
    close_kb = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="« Закрыть лог", callback_data=f"logclose:{chat_id}")
    ]])

    sent_ids = []
    for i, chunk in enumerate(chunks):
        sent = await callback.message.answer(chunk, reply_markup=close_kb if i == len(chunks) - 1 else None)
        sent_ids.append(sent.message_id)

    ADMIN_LOG_VIEW["chat_id"] = chat_id
    ADMIN_LOG_VIEW["message_ids"] = sent_ids
    await callback.answer()


@dp.callback_query(F.data.startswith("logclose:"))
async def cb_log_close(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    for message_id in ADMIN_LOG_VIEW["message_ids"]:
        try:
            await bot.delete_message(ADMIN_ID, message_id)
        except Exception:
            pass
    ADMIN_LOG_VIEW["chat_id"] = None
    ADMIN_LOG_VIEW["message_ids"] = []
    await callback.answer("Закрыто")


# --- Уведомление о новом чате ---

@dp.callback_query(F.data.startswith("newchat_add:"))
async def cb_newchat_add(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    chat_id = int(callback.data.split(":")[1])
    database.add_allowed_chat(chat_id)
    database.log_activity(chat_id, "Добавлен в разрешенные администратором (уведомление о новом чате)")
    await update_allowed_chats_cache()
    await safe_edit_text(callback.message,"✅ Добавлен", reply_markup=admin_close_keyboard())
    await callback.answer()


@dp.callback_query(F.data.startswith("newchat_skip:"))
async def cb_newchat_skip(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await safe_edit_text(callback.message,"🚫 Не добавлен", reply_markup=admin_close_keyboard())
    await callback.answer()


# --- Уведомление о смене статуса клиента ---

@dp.callback_query(F.data.startswith("statuskeep:"))
async def cb_status_keep(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await safe_edit_text(callback.message,
        "✅ Изменение статуса подтверждено.", reply_markup=admin_close_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("statusrevert:"))
async def cb_status_revert(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    _, chat_id_str, old_status = callback.data.split(":")
    chat_id = int(chat_id_str)
    database.update_profile_status(chat_id, old_status)
    database.log_activity(chat_id, f"Статус возвращен администратором на «{old_status}»")
    await safe_edit_text(callback.message,
        f"↩️ Статус возвращен на «{old_status}».", reply_markup=admin_close_keyboard()
    )
    await callback.answer()


@dp.message(F.text)
async def admin_search_input(message: types.Message):
    """Ловит ответ на '🔎 Найти по юзернейму' или '🔢 Ввести chat_id вручную', пока ADMIN_STATE
    не сброшен. Если ни одно состояние не активно — просто пропускает сообщение (без хендлера)."""
    if message.from_user.id != ADMIN_ID:
        return

    if ADMIN_STATE["waiting_chat_id"]:
        ADMIN_STATE["waiting_chat_id"] = False
        raw = message.text.strip()
        try:
            chat_id = int(raw)
        except ValueError:
            await message.answer(f"«{raw}» — это не похоже на числовой chat_id. Отменено.", reply_markup=admin_close_keyboard())
            return
        database.add_allowed_chat(chat_id)
        database.log_activity(chat_id, "Добавлен в разрешенные администратором (по chat_id вручную)")
        await update_allowed_chats_cache()
        await message.answer(f"✅ Чат {chat_id} добавлен в разрешенные.", reply_markup=admin_close_keyboard())
        return

    if ADMIN_STATE["waiting_username"]:
        ADMIN_STATE["waiting_username"] = False
        query = message.text.strip().lstrip("@")
        text, kb = await render_chats_picker(0, search=query)
        await message.answer(text, reply_markup=kb)


# --- ОБРАБОТЧИК БИЗНЕС-СООБЩЕНИЙ (РЕЖИМ СЕКРЕТАРЯ) ---

@dp.business_message(F.text)
async def handle_business_message(message: types.Message):
    """Слушает сообщения, поступающие на твой личный Telegram-аккаунт."""
    print(f"[BUSINESS_MSG] Апдейт получен: chat_id={message.chat.id}, from_user_id={message.from_user.id}, "
          f"msg_id={message.message_id}, text={message.text!r}")
    try:
        await _handle_business_message_impl(message)
    except Exception:
        # Любая необработанная ошибка здесь раньше "тихо" роняла обработку одного сообщения —
        # теперь всегда печатаем полный traceback, чтобы сразу было видно, что и где сломалось.
        print(f"[BUSINESS_MSG] !!! ИСКЛЮЧЕНИЕ при обработке chat_id={message.chat.id}:")
        traceback.print_exc()


async def _handle_business_message_impl(message: types.Message):
    # Владелец аккаунта написал сам (вручную вмешался в разговор) — не отвечаем ИИ поверх,
    # но учитываем его сообщение в истории и таймерах.
    if message.from_user.id == ADMIN_ID:
        chat_id = message.chat.id
        print(f"[BUSINESS_MSG] {chat_id}: это владелец аккаунта пишет вручную — ИИ не отвечает, только логируем")
        # Берем имя/юзернейм из message.chat (это клиент), а не message.from_user (это сам владелец) —
        # иначе адресная книга затрется именем владельца при его ручном вмешательстве
        database.upsert_known_contact(chat_id, message.chat.username, message.chat.first_name)

        # Владелец уже взял разговор на себя — автоответ ИИ по старым сообщениям клиента не нужен
        if chat_id in USER_MESSAGE_TASKS and not USER_MESSAGE_TASKS[chat_id].done():
            print(f"[BUSINESS_MSG] {chat_id}: отменяю висящий таймер дебаунса клиента")
            USER_MESSAGE_TASKS[chat_id].cancel()

        # Сохраняем еще не улетевшие в БД куски от клиента, но без ответа ИИ на них
        buf = USER_MESSAGE_BUFFERS.pop(chat_id, None)
        if buf:
            print(f"[BUSINESS_MSG] {chat_id}: сохраняю {len(buf['parts'])} неотправленных кусков клиента без ответа ИИ")
            for part in buf["parts"]:
                database.save_message(chat_id, "user", part["text"], tg_message_id=part["message_id"])
                track_message_for_summary(chat_id)

        database.save_message(chat_id, "me", message.text, tg_message_id=message.message_id)
        track_message_for_summary(chat_id)
        LAST_OUR_MESSAGE_TIME[chat_id] = datetime.now()
        LAST_OUR_MESSAGE_ID[chat_id] = message.message_id
        return

    chat_id = message.chat.id
    biz_conn_id = message.business_connection_id

    # СОХРАНЯЕМ ID СОЕДИНЕНИЯ В БАЗУ!
    database.save_biz_conn_id(chat_id, biz_conn_id)
    database.upsert_known_contact(chat_id, message.chat.username, message.chat.first_name)
    print(f"[BUSINESS_MSG] {chat_id}: biz_conn_id сохранен, адресная книга обновлена")
    user_text = message.text

    # Проверяем, является ли сообщение ответом на другое сообщение
    if message.reply_to_message and message.reply_to_message.text:
        # Обрезаем цитируемый текст
        quoted_text = message.reply_to_message.text
        if len(quoted_text) > 40:
            quoted_text = quoted_text[:40] + "..."

        # Оборачиваем текст в маркер для ИИ
        user_text = f'[Ответ на сообщение: "{quoted_text}"] {user_text}'

    if chat_id not in ALLOWED_CHATS_CACHE:
        print(f"[BUSINESS_MSG] {chat_id}: чат НЕ в ALLOWED_CHATS_CACHE ({sorted(ALLOWED_CHATS_CACHE)}) — ИИ не отвечает")
        # Вместо тихого игнора один раз пингуем админа о новом человеке
        contact = database.get_known_contact(chat_id)
        if contact and not contact[4]:  # admin_notified
            database.mark_admin_notified(chat_id)
            label = format_contact_label(chat_id, contact[1], contact[2])
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[
                types.InlineKeyboardButton(text="✅ Добавить", callback_data=f"newchat_add:{chat_id}"),
                types.InlineKeyboardButton(text="🚫 Не добавлять", callback_data=f"newchat_skip:{chat_id}"),
            ]])
            await bot.send_message(ADMIN_ID, f"Новый чат: {label}. Добавить в разрешенные?", reply_markup=keyboard)
        return

    print(f"[BUSINESS_MSG] {chat_id}: чат разрешен, добавляю сообщение в буфер")
    if chat_id not in USER_MESSAGE_BUFFERS:
        USER_MESSAGE_BUFFERS[chat_id] = {
            "business_connection_id": message.business_connection_id,
            "parts": [],
        }

    # Накапливаем кусочки, чтобы их можно было точечно патчить/удалять до сохранения в БД
    USER_MESSAGE_BUFFERS[chat_id]["parts"].append({"message_id": message.message_id, "text": user_text})
    print(f"[BUSINESS_MSG] {chat_id}: в буфере теперь {len(USER_MESSAGE_BUFFERS[chat_id]['parts'])} куск(ов)")

    # Отменяем предыдущий таймер, если клиент продолжает писать
    if chat_id in USER_MESSAGE_TASKS and not USER_MESSAGE_TASKS[chat_id].done():
        print(f"[BUSINESS_MSG] {chat_id}: отменяю предыдущий таймер дебаунса")
        USER_MESSAGE_TASKS[chat_id].cancel()

    async def _debounce_timer():
        try:
            print(f"[DEBOUNCE] {chat_id}: таймер запущен, жду {DEBOUNCE_TIME} сек")
            await asyncio.sleep(DEBOUNCE_TIME)

            # Буфер мог исчезнуть, если за это время владелец вручную вмешался в разговор
            # (см. ветку message.from_user.id == ADMIN_ID выше) — тогда просто выходим
            buf = USER_MESSAGE_BUFFERS.get(chat_id)
            if not buf:
                print(f"[DEBOUNCE] {chat_id}: буфер уже пуст (перехвачен владельцем?), выхожу без ответа")
                return

            # Эвристика: проверяем, завершена ли мысль (точки, вопросы, восклицательные знаки)
            parts = buf["parts"]
            last_text = parts[-1]["text"] if parts else ""
            last_part = last_text.strip().split()[-1] if last_text else ""
            total_len = sum(len(p["text"]) for p in parts)
            if last_part.endswith((".", "?", "!")) and total_len > 20:
                print(f"[DEBOUNCE] {chat_id}: мысль завершена ({total_len} симв.), обрабатываю сразу")
                await process_user_message_buffer(chat_id)
                return

            # Если мысль кажется незавершенной, ждем до MAX_WAIT_TIME
            print(f"[DEBOUNCE] {chat_id}: мысль не завершена, жду еще {MAX_WAIT_TIME - DEBOUNCE_TIME} сек")
            await asyncio.sleep(MAX_WAIT_TIME - DEBOUNCE_TIME)
            await process_user_message_buffer(chat_id)

        except asyncio.CancelledError:
            print(f"[DEBOUNCE] {chat_id}: таймер отменен (пришло новое сообщение)")
        except Exception:
            print(f"[DEBOUNCE] !!! ИСКЛЮЧЕНИЕ в таймере для чата {chat_id}:")
            traceback.print_exc()

    # Запускаем новый таймер ожидания
    USER_MESSAGE_TASKS[chat_id] = asyncio.create_task(_debounce_timer())
    print(f"[BUSINESS_MSG] {chat_id}: новый таймер дебаунса запущен")


@dp.edited_business_message(F.text)
async def handle_edited_business_message(message: types.Message):
    """Ловит правки уже отправленных собеседником (или нами) сообщений."""
    chat_id = message.chat.id
    tg_id = message.message_id
    new_text = message.text

    buf = USER_MESSAGE_BUFFERS.get(chat_id)
    if buf:
        for part in buf["parts"]:
            if part["message_id"] == tg_id:
                part["text"] = new_text
                return  # ещё не сохранено в БД, патчим на месте

    # Сообщение уже улетело в БД и, возможно, уже было отвечено — правим историю для следующего хода ИИ
    database.update_message_text_by_tg_id(chat_id, tg_id, new_text)


@dp.deleted_business_messages()
async def handle_deleted_business_messages(event: types.BusinessMessagesDeleted):
    """Ловит удаление сообщений собеседником и убирает их из буфера/БД."""
    chat_id = event.chat.id
    buf = USER_MESSAGE_BUFFERS.get(chat_id)
    for tg_id in event.message_ids:
        if buf:
            buf["parts"] = [p for p in buf["parts"] if p["message_id"] != tg_id]
        database.mark_message_deleted(chat_id, tg_id)


@dp.error()
async def global_error_handler(event: types.ErrorEvent):
    """Ловит абсолютно любое необработанное исключение в любом хендлере (message, callback_query,
    business_message и т.д.) — чтобы ни одна ошибка не проходила через консоль незамеченной."""
    print(f"[GLOBAL ERROR] Необработанное исключение при обработке апдейта {event.update.update_id}:")
    traceback.print_exc()
    return True


async def initiative_worker():
    """Фоновый цикл, который проверяет, не пора ли написать первой."""
    while True:
        await asyncio.sleep(60) # Проверяем каждую минуту
        now = datetime.now()
        print(f"Фоновая инициатива: проверка разрешенных чатов в {now.strftime('%H:%M:%S')}...")

        for chat_id in list(ALLOWED_CHATS_CACHE):
            try:
                busy_until = USER_BUSY_UNTIL.get(chat_id)
                last_our = LAST_OUR_MESSAGE_TIME.get(chat_id, now)
                last_user = LAST_USER_MESSAGE_TIME.get(chat_id, now)

                # СЦЕНАРИЙ 1: Бот освободился после дел (сна/душа) и обещал вернуться
                if busy_until and now >= busy_until:
                    if chat_id in USER_PENDING_RETURN:
                        activity = USER_PENDING_RETURN.pop(chat_id)
                        del USER_BUSY_UNTIL[chat_id]

                        # Формируем системный пинок для ИИ
                        prompt_injection = f"[СИСТЕМНОЕ СООБЩЕНИЕ: Ты только что вернулась из статуса '{activity}'. Напиши клиенту об этом первой. Если он писал тебе, пока тебя не было, обязательно ответь на его сообщения.]"
                        print(f"Инициатива: Бот возвращается из '{activity}' в чат {chat_id}")
                        database.log_activity(chat_id, f"Вернулась из '{activity}', пишет первой")
                        await trigger_proactive_ai(chat_id, prompt_injection)
                    else:
                        # Просто освободились, но писать первыми не обещали
                        del USER_BUSY_UNTIL[chat_id]

                # СЦЕНАРИЙ 2: Прошел 1 час тишины (Инициатива или Игнор)
                if not busy_until and (now - last_our).total_seconds() > 3600: # 1 час
                    if (now - last_user).total_seconds() > 3600:
                        # Никто ничего не писал 1 час.
                        LAST_OUR_MESSAGE_TIME[chat_id] = now
                        prompt_injection = "[СИСТЕМНОЕ СООБЩЕНИЕ: В чате тишина уже 1 час. Если диалог повис (например, ты задала вопрос, а он не ответил), напиши что-то вроде 'почему игноришь?'. Если диалог логически завершился ранее, просто спроси 'как дела?' или 'что делаешь?'. Если вы попрощались, НЕ ПИШИ НИЧЕГО (вызови функцию без текста).]"
                        print(f"Инициатива: Проверка тишины (1 час) для {chat_id}")
                        database.log_activity(chat_id, "Инициатива: тишина 1 час, написали первыми")
                        await trigger_proactive_ai(chat_id, prompt_injection)
            except Exception:
                print(f"[INITIATIVE] !!! ИСКЛЮЧЕНИЕ при обработке чата {chat_id} (остальные чаты продолжат обрабатываться):")
                traceback.print_exc()


def is_active_streak(rows, window_minutes: float, min_count: int) -> bool:
    """True, если в выборке есть min_count+ сообщений, уместившихся в window_minutes минут."""
    if len(rows) < min_count:
        return False

    first_ts = rows[0][2]
    last_ts = rows[-1][2]
    if isinstance(first_ts, str):
        first_ts = datetime.strptime(first_ts, "%Y-%m-%d %H:%M:%S")
    if isinstance(last_ts, str):
        last_ts = datetime.strptime(last_ts, "%Y-%m-%d %H:%M:%S")

    return (last_ts - first_ts).total_seconds() <= window_minutes * 60


async def typo_scheduler_worker():
    """Фоновый цикл: 2-3 раза в день ловит активный диалог и планирует "живую" опечатку."""
    global LAST_TYPO_EVENT_AT
    while True:
        await asyncio.sleep(300)  # проверка раз в 5 минут
        try:
            now = datetime.now()

            if TYPO_DAILY_STATE["date"] != now.date():
                TYPO_DAILY_STATE["date"] = now.date()
                TYPO_DAILY_STATE["quota"] = random.randint(2, 3)
                TYPO_DAILY_STATE["used"] = 0

            if TYPO_DAILY_STATE["used"] >= TYPO_DAILY_STATE["quota"]:
                continue
            if LAST_TYPO_EVENT_AT and (now - LAST_TYPO_EVENT_AT).total_seconds() < TYPO_COOLDOWN_MIN * 60:
                continue

            candidates = []
            for chat_id in list(ALLOWED_CHATS_CACHE):
                recent = database.get_recent_messages_with_time(chat_id, limit=TYPO_MIN_STREAK)
                if is_active_streak(recent, window_minutes=TYPO_ACTIVE_WINDOW_MIN, min_count=TYPO_MIN_STREAK):
                    candidates.append(chat_id)

            if candidates:
                chosen = random.choice(candidates)
                PENDING_TYPO_CHATS.add(chosen)
                TYPO_DAILY_STATE["used"] += 1
                LAST_TYPO_EVENT_AT = now
                print(f"Опечатка запланирована для чата {chosen} ({TYPO_DAILY_STATE['used']}/{TYPO_DAILY_STATE['quota']} за сегодня).")
        except Exception:
            print("[TYPO_SCHEDULER] !!! ИСКЛЮЧЕНИЕ в фоновом цикле опечаток (цикл продолжит работу):")
            traceback.print_exc()

# --- ЗАПУСК БОТА ---

WSL_DISTRO = "Ubuntu-22.04"
LOCAL_SERVER_STARTUP_TIMEOUT = 180  # секунд на загрузку модели в WSL
LOCAL_SERVER_PROCESS = None


def start_local_model_server():
    """Поднимает serve_model.py в WSL и ждёт, пока модель загрузится и порт откликнется.
    Успешный TCP-коннект = сервер уже прошёл load_model() (он биндит порт только после neё)."""
    global LOCAL_SERVER_PROCESS
    parsed = urlparse(LOCAL_MODEL_URL)
    host, port = parsed.hostname, parsed.port

    print(f"Запускаю локальный сервер модели в WSL ({host}:{port})...")
    log_file = open('serve_model.log', 'a', encoding='utf-8')
    LOCAL_SERVER_PROCESS = subprocess.Popen(
        ['wsl', '-d', WSL_DISTRO, '--', 'bash', '-c', 'cd /mnt/d/FISHBOT && python3 serve_model.py'],
        stdout=log_file, stderr=subprocess.STDOUT,
    )

    deadline = time.monotonic() + LOCAL_SERVER_STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if LOCAL_SERVER_PROCESS.poll() is not None:
            raise RuntimeError(
                f"serve_model.py завершился раньше времени (код {LOCAL_SERVER_PROCESS.returncode}). "
                "Смотри serve_model.log."
            )
        try:
            with socket.create_connection((host, port), timeout=1):
                print("Локальный сервер модели готов.")
                return
        except OSError:
            time.sleep(1)

    raise RuntimeError(
        f"Локальный сервер модели не поднялся за {LOCAL_SERVER_STARTUP_TIMEOUT} сек. Смотри serve_model.log."
    )


def stop_local_model_server():
    if LOCAL_SERVER_PROCESS and LOCAL_SERVER_PROCESS.poll() is None:
        print("Останавливаю локальный сервер модели...")
        LOCAL_SERVER_PROCESS.terminate()
        try:
            LOCAL_SERVER_PROCESS.wait(timeout=10)
        except subprocess.TimeoutExpired:
            LOCAL_SERVER_PROCESS.kill()


async def main():
    print("Инициализация базы данных...")
    database.init_db()

    if INFERENCE_BACKEND == "local":
        start_local_model_server()

    print("Обновление кеша разрешенных чатов...")
    await update_allowed_chats_cache()

    # ЗАПУСКАЕМ ФОНОВУЮ ИНИЦИАТИВУ
    asyncio.create_task(initiative_worker())
    asyncio.create_task(typo_scheduler_worker())

    print(f"Бот успешно запущен в режиме Telegram Business. Инференс: {INFERENCE_BACKEND}.")
    try:
        await dp.start_polling(bot)
    finally:
        stop_local_model_server()

if __name__ == '__main__':
    asyncio.run(main())