import json
import os
import asyncio
import socket
import subprocess
import time
from urllib.parse import urlparse
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.enums import ChatAction
import database
from ai_backends import ai_client, TOOLS, generate_reply, INFERENCE_BACKEND, LOCAL_MODEL_URL
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

async def send_admin_message(message_text: str):
    """Отправляет сообщение администратору напрямую в бота."""
    try:
        await bot.send_message(ADMIN_ID, message_text)
    except Exception as e:
        print(f"Ошибка при отправке сообщения администратору: {e}")

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
    await send_admin_message(f"Начинаю генерацию сводки и статуса для чата {chat_id}...")

    recent_msgs = database.get_recent_messages(chat_id, limit=20)
    
    if not recent_msgs:
        print(f"В чате {chat_id} нет сообщений для сводки.")
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

    messages_for_ai = [{"role": "system", "content": summary_and_status_prompt}]
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
                database.update_profile_summary(chat_id, summary_text)
                database.update_profile_status(chat_id, status_text)
                print(f"Для чата {chat_id} обновлены сводка: \"{summary_text}\", и статус: \"{status_text}\".")
                await send_admin_message(f"Для чата {chat_id} обновлены сводка: \"{summary_text}\", и статус: \"{status_text}\".")
        else:
            print(f"ИИ не использовал функцию для обновления профиля.")

    except Exception as e:
        print(f"Ошибка при генерации сводки для чата {chat_id}: {e}")

async def get_ai_response(chat_id: int):
    """Формирует контекст и запрашивает ответ у активного бэкенда (OpenAI или локальная модель)."""
    # Узнаем текущее время
    current_time = datetime.now().strftime("%H:%M")
    dynamic_extra = f"[СИСТЕМНОЕ ВРЕМЯ СЕЙЧАС: {current_time}. Если сейчас от 00:00 до 02:00, и диалог логически завершен, попрощайся (пожелай спокойной ночи) и ВЫЗОВИ функцию change_activity на 480-600 минут. Если ты идешь работать или в душ, тоже вызови эту функцию.]"

    profile_summary = database.get_profile_summary(chat_id)
    if profile_summary:
        dynamic_extra += f"\n\n[Предыдущая сводка беседы с этим клиентом: {profile_summary}]"

    messages_for_ai = [{"role": "system", "content": SYSTEM_PROMPT + "\n\n" + dynamic_extra}]

    recent_msgs = database.get_recent_messages(chat_id, limit=8)
    history_pairs = [
        {"role": "assistant" if msg[0] == "me" else "user", "content": msg[1]}
        for msg in recent_msgs
    ]
    messages_for_ai.extend(history_pairs)

    try:
        content, tool_calls, tool_decision_failed = await generate_reply(
            messages_for_ai, dynamic_extra, history_pairs
        )
    except Exception as e:
        print(f"Ошибка при получении ответа ИИ для чата {chat_id}: {e}")
        await send_admin_message(f"Ошибка при получении ответа ИИ для чата {chat_id}: {e}")
        return None, None

    if tool_decision_failed:
        await send_admin_message(
            f"Для чата {chat_id}: статус/активность не обновились (сбой decide_tools), но ответ отправлен."
        )

    return content, tool_calls


async def split_and_send_messages(chat_id: int, text: str, biz_conn_id: str, reply_to_msg_id: int = None):
    """Разделяет текст и отправляет от лица бизнес-аккаунта с имитацией набора текста."""
    parts = [p.strip() for p in text.split('\n') if p.strip()]
    if not parts or (len(parts) == 1 and len(parts[0]) < 50):
        parts = [text] 

    for i, part in enumerate(parts):
        if not part:
            continue
        
        # Передаем business_connection_id, чтобы печатать от имени личного аккаунта
        await bot.send_chat_action(chat_id, ChatAction.TYPING, business_connection_id=biz_conn_id)
        await asyncio.sleep(len(part) * 0.1) # Задержка, зависящая от длины текста
        
        if i == 0 and reply_to_msg_id:
            await bot.send_message(chat_id, part, business_connection_id=biz_conn_id, reply_to_message_id=reply_to_msg_id)
        else:
            await bot.send_message(chat_id, part, business_connection_id=biz_conn_id)

async def process_user_message_buffer(chat_id: int):
    global USER_MESSAGE_BUFFERS, USER_MESSAGE_TASKS, USER_BUSY_UNTIL, LAST_OUR_MESSAGE_TIME

    if chat_id not in USER_MESSAGE_BUFFERS or not USER_MESSAGE_BUFFERS[chat_id]["parts"]:
        return

    buffer_data = USER_MESSAGE_BUFFERS.pop(chat_id)
    parts = buffer_data["parts"]
    user_full_text = " ".join(p["text"] for p in parts).strip()
    biz_conn_id = buffer_data["business_connection_id"]
    original_message_id = parts[0]["message_id"]

    now = datetime.now()

    # 1. ПРОВЕРКА НА СОН/ЗАНЯТОСТЬ
    if chat_id in USER_BUSY_UNTIL and now < USER_BUSY_UNTIL[chat_id]:
        print(f"Девушка занята до {USER_BUSY_UNTIL[chat_id]}. Сообщение сохранено, но ответа пока не будет.")
        for part in parts:
            database.save_message(chat_id, "user", part["text"], tg_message_id=part["message_id"])
        return # Просто сохраняем в БД и прерываем обработку!

    # 2. ДИНАМИЧЕСКИЕ ПАУЗЫ (Быстро в диалоге, медленно после молчания)
    last_msg_time = LAST_OUR_MESSAGE_TIME.get(chat_id)
    if last_msg_time:
        time_since_last = (now - last_msg_time).total_seconds()
        if time_since_last > 600: # Если молчали больше 10 минут
            # Имитируем, что телефон не в руках. Задержка от 3 до 8 минут
            delay = random.randint(180, 480)
            print(f"Диалог возобновлен. Ждем {delay} сек перед ответом...")
            await asyncio.sleep(delay)

    # Сохраняем каждый кусочек отдельной строкой (точная привязка к tg_message_id) и получаем ответ ИИ
    for part in parts:
        database.save_message(chat_id, "user", part["text"], tg_message_id=part["message_id"])
    await bot.send_chat_action(chat_id, ChatAction.TYPING, business_connection_id=biz_conn_id)
    
    # Здесь нужно немного изменить get_ai_response, чтобы он мог возвращать вызовы функций!
    ai_reply, tool_calls = await get_ai_response(chat_id)

    # 3. ОБРАБОТКА РЕШЕНИЯ ИИ ПОЙТИ СПАТЬ/ПО ДЕЛАМ
    if tool_calls:
        for tool in tool_calls:
            if tool.function.name == "change_activity":
                args = json.loads(tool.function.arguments)
                busy_minutes = args.get("minutes", 0)
                activity = args.get("activity", "дела")
                
                USER_BUSY_UNTIL[chat_id] = datetime.now() + timedelta(minutes=busy_minutes)
                print(f"ИИ решил уйти в статус: {activity} на {busy_minutes} минут.")
                
                # Если ИИ ничего не написал текстом, а просто ушел спать
                if not ai_reply:
                    return

    if ai_reply:
        database.save_message(chat_id, "me", ai_reply)
        await split_and_send_messages(chat_id, ai_reply, biz_conn_id, reply_to_msg_id=original_message_id)
        LAST_OUR_MESSAGE_TIME[chat_id] = datetime.now() 


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


# --- ОБРАБОТЧИК БИЗНЕС-СООБЩЕНИЙ (РЕЖИМ СЕКРЕТАРЯ) ---

@dp.business_message(F.text)
async def handle_business_message(message: types.Message):
    """Слушает сообщения, поступающие на твой личный Telegram-аккаунт."""

    # Владелец аккаунта написал сам (вручную вмешался в разговор) — не отвечаем ИИ поверх,
    # но учитываем его сообщение в истории и таймерах.
    if message.from_user.id == ADMIN_ID:
        chat_id = message.chat.id

        # Владелец уже взял разговор на себя — автоответ ИИ по старым сообщениям клиента не нужен
        if chat_id in USER_MESSAGE_TASKS and not USER_MESSAGE_TASKS[chat_id].done():
            USER_MESSAGE_TASKS[chat_id].cancel()

        # Сохраняем еще не улетевшие в БД куски от клиента, но без ответа ИИ на них
        buf = USER_MESSAGE_BUFFERS.pop(chat_id, None)
        if buf:
            for part in buf["parts"]:
                database.save_message(chat_id, "user", part["text"], tg_message_id=part["message_id"])

        database.save_message(chat_id, "me", message.text, tg_message_id=message.message_id)
        LAST_OUR_MESSAGE_TIME[chat_id] = datetime.now()
        LAST_OUR_MESSAGE_ID[chat_id] = message.message_id
        return

    chat_id = message.chat.id
    biz_conn_id = message.business_connection_id
    
    # СОХРАНЯЕМ ID СОЕДИНЕНИЯ В БАЗУ!
    database.save_biz_conn_id(chat_id, biz_conn_id)
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
        return

    if chat_id not in USER_MESSAGE_BUFFERS:
        USER_MESSAGE_BUFFERS[chat_id] = {
            "business_connection_id": message.business_connection_id,
            "parts": [],
        }

    # Накапливаем кусочки, чтобы их можно было точечно патчить/удалять до сохранения в БД
    USER_MESSAGE_BUFFERS[chat_id]["parts"].append({"message_id": message.message_id, "text": user_text})

    # Отменяем предыдущий таймер, если клиент продолжает писать
    if chat_id in USER_MESSAGE_TASKS and not USER_MESSAGE_TASKS[chat_id].done():
        USER_MESSAGE_TASKS[chat_id].cancel()

    async def _debounce_timer():
        try:
            await asyncio.sleep(DEBOUNCE_TIME)

            # Эвристика: проверяем, завершена ли мысль (точки, вопросы, восклицательные знаки)
            parts = USER_MESSAGE_BUFFERS[chat_id]["parts"]
            last_text = parts[-1]["text"] if parts else ""
            last_part = last_text.strip().split()[-1] if last_text else ""
            total_len = sum(len(p["text"]) for p in parts)
            if last_part.endswith((".", "?", "!")) and total_len > 20:
                await process_user_message_buffer(chat_id)
                return

            # Если мысль кажется незавершенной, ждем до MAX_WAIT_TIME
            await asyncio.sleep(MAX_WAIT_TIME - DEBOUNCE_TIME)
            await process_user_message_buffer(chat_id)

        except asyncio.CancelledError:
            pass # Таймер отменен, так как пришло новое сообщение
        except Exception as e:
            print(f"Ошибка в _debounce_timer для чата {chat_id}: {e}")

    # Запускаем новый таймер ожидания
    USER_MESSAGE_TASKS[chat_id] = asyncio.create_task(_debounce_timer())


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


async def initiative_worker():
    """Фоновый цикл, который проверяет, не пора ли написать первой."""
    while True:
        await asyncio.sleep(60) # Проверяем каждую минуту
        now = datetime.now()
        print(f"Фоновая инициатива: проверка разрешенных чатов в {now.strftime('%H:%M:%S')}...")

        for chat_id in list(ALLOWED_CHATS_CACHE):
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
                    await trigger_proactive_ai(chat_id, prompt_injection)

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

    print(f"Бот успешно запущен в режиме Telegram Business. Инференс: {INFERENCE_BACKEND}.")
    try:
        await dp.start_polling(bot)
    finally:
        stop_local_model_server()

if __name__ == '__main__':
    asyncio.run(main())