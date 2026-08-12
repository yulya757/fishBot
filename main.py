import json
import os
import asyncio
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.enums import ChatAction
import httpx
from openai import AsyncOpenAI
import database
from datetime import datetime, timedelta
import random

# Загружаем ключи из .env
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
ADMIN_ID = int(os.getenv('ADMIN_ID'))

# Кеш для разрешенных чатов
ALLOWED_CHATS_CACHE = set() 
USER_BUSY_UNTIL = {}           # {chat_id: datetime} - до скольки бот занят
USER_PENDING_RETURN = {}       # {chat_id: "activity_name"} - откуда бот должен вернуться
LAST_OUR_MESSAGE_TIME = {}     # {chat_id: datetime} - когда мы писали в последний раз
LAST_USER_MESSAGE_TIME = {}    # {chat_id: datetime} - когда юзер писал в последний раз

# Инициализируем бота и диспетчер (aiogram)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


http_client = httpx.AsyncClient(
    trust_env=True,  # Подтягивает настройки системного VPN/прокси Windows
    timeout=60.0     # Увеличиваем таймаут, чтобы VPN успевал ответить
)

ai_client = AsyncOpenAI(
    api_key=OPENAI_API_KEY,
    http_client=http_client
)

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

# Описание инструмента для OpenAI
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "update_profile_data",
            "description": "Обновляет сводку беседы и статус готовности клиента к покупке.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Краткая сводка беседы с клиентом (1-2 предложения)."
                    },
                    "status": {
                        "type": "string",
                        "enum": ["new", "cold", "warm", "ready", "rejected"],
                        "description": "Текущий статус готовности клиента к покупке."
                    }
                },
                "required": ["summary", "status"],
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
    recent_msgs = database.get_recent_messages(chat_id, limit=8)
    messages_for_ai = [{"role": "system", "content": SYSTEM_PROMPT + "\n\n" + context_prompt}]
    
    for msg in recent_msgs:
        role = "assistant" if msg[0] == "me" else "user"
        messages_for_ai.append({"role": role, "content": msg[1]})
        
    try:
        # 3. Делаем запрос в OpenAI
        response = await ai_client.chat.completions.create(
            model="gpt-4o",
            messages=messages_for_ai,
            tools=TOOLS,
            max_tokens=150
        )
        
        ai_reply = response.choices[0].message.content
        
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
    """Формирует контекст и делает запрос к OpenAI (gpt-4o)."""
    # Узнаем текущее время
    current_time = datetime.now().strftime("%H:%M")
    dynamic_prompt = SYSTEM_PROMPT + f"\n\n[СИСТЕМНОЕ ВРЕМЯ СЕЙЧАС: {current_time}. Если сейчас от 00:00 до 02:00, и диалог логически завершен, попрощайся (пожелай спокойной ночи) и ВЫЗОВИ функцию change_activity на 480-600 минут. Если ты идешь работать или в душ, тоже вызови эту функцию.]"
    messages_for_ai = [{"role": "system", "content": dynamic_prompt}]

    profile_summary = database.get_profile_summary(chat_id)
    if profile_summary:
        messages_for_ai.append({"role": "system", "content": f"[Предыдущая сводка беседы с этим клиентом: {profile_summary}]"})

    recent_msgs = database.get_recent_messages(chat_id, limit=8)
    for msg in recent_msgs:
        role = "assistant" if msg[0] == "me" else "user"
        messages_for_ai.append({"role": role, "content": msg[1]})

    response = await ai_client.chat.completions.create(
        model="gpt-4o",
        messages=messages_for_ai,
        tools=TOOLS, # <--- Добавили инструменты сюда
        max_tokens=150
    )
        
    msg = response.choices[0].message
    return msg.content, msg.tool_calls


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

    if chat_id not in USER_MESSAGE_BUFFERS or not USER_MESSAGE_BUFFERS[chat_id]["text"]:
        return

    buffer_data = USER_MESSAGE_BUFFERS.pop(chat_id)
    user_full_text = buffer_data["text"].strip()
    biz_conn_id = buffer_data["business_connection_id"]
    original_message_id = buffer_data["message_id"]

    now = datetime.now()

    # 1. ПРОВЕРКА НА СОН/ЗАНЯТОСТЬ
    if chat_id in USER_BUSY_UNTIL and now < USER_BUSY_UNTIL[chat_id]:
        print(f"Девушка занята до {USER_BUSY_UNTIL[chat_id]}. Сообщение сохранено, но ответа пока не будет.")
        database.save_message(chat_id, "user", user_full_text)
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
    
    # Сохраняем в БД и получаем ответ ИИ
    database.save_message(chat_id, "user", user_full_text)
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
    
    # Игнорируем твои собственные исходящие сообщения, чтобы бот не отвечал сам себе
    if message.from_user.id == ADMIN_ID:
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
            "text": "",
            "business_connection_id": message.business_connection_id,
            "message_id": message.message_id
        }

    # Накапливаем текст с учетом нашей новой пометки
    USER_MESSAGE_BUFFERS[chat_id]["text"] += " " + user_text

    # Отменяем предыдущий таймер, если клиент продолжает писать
    if chat_id in USER_MESSAGE_TASKS and not USER_MESSAGE_TASKS[chat_id].done():
        USER_MESSAGE_TASKS[chat_id].cancel()

    async def _debounce_timer():
        try:
            await asyncio.sleep(DEBOUNCE_TIME)
            
            # Эвристика: проверяем, завершена ли мысль (точки, вопросы, восклицательные знаки)
            last_part = USER_MESSAGE_BUFFERS[chat_id]["text"].strip().split()[-1] if USER_MESSAGE_BUFFERS[chat_id]["text"] else ""
            if last_part.endswith((".", "?", "!")) and len(USER_MESSAGE_BUFFERS[chat_id]["text"]) > 20:
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

async def main():
    print("Инициализация базы данных...")
    database.init_db()
    
    print("Обновление кеша разрешенных чатов...")
    await update_allowed_chats_cache()

    # ЗАПУСКАЕМ ФОНОВУЮ ИНИЦИАТИВУ
    asyncio.create_task(initiative_worker())

    print("Бот успешно запущен в режиме Telegram Business на моделях OpenAI!")
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())