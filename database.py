import json
import sqlite3

def init_db():
    """Создает таблицы при первом запуске, если их нет"""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    
    # Таблица для сообщений (Краткосрочная память)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            sender TEXT,
            text TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Таблица для профилей (Долгосрочная память)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS profiles (
            chat_id INTEGER PRIMARY KEY,
            summary TEXT,
            status TEXT DEFAULT 'новый'
        )
    ''')

    # Миграция: колонки для отслеживания правок/удалений сообщений собеседника
    try:
        cursor.execute('ALTER TABLE messages ADD COLUMN tg_message_id INTEGER')
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute('ALTER TABLE messages ADD COLUMN deleted INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass

    # Миграция: расширенный структурированный профиль клиента (долгосрочная память),
    # соответствует JSON-схеме из summary_prompt.txt / TOOLS["update_profile_data"]
    for column, coltype in [
        ("name", "TEXT"), ("age", "INTEGER"), ("location", "TEXT"), ("occupation", "TEXT"),
        ("financial_status", "TEXT"), ("pain_points", "TEXT"), ("personal_facts", "TEXT"),
        ("next_suggested_topic", "TEXT"),
    ]:
        try:
            cursor.execute(f'ALTER TABLE profiles ADD COLUMN {column} {coltype}')
        except sqlite3.OperationalError:
            pass

    # Адресная книга: пассивно наполняется из каждого входящего сообщения,
    # независимо от того, разрешен чат или нет
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS known_contacts (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
            admin_notified INTEGER DEFAULT 0
        )
    ''')
    try:
        cursor.execute('ALTER TABLE known_contacts ADD COLUMN admin_notified INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass

    # Лог действий по чату (активность/статус/т.д.) для панели админа
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            event TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.commit()
    conn.close()

def save_message(chat_id, sender, text, tg_message_id=None):
    """Сохраняет новое сообщение в базу."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    cursor.execute('INSERT INTO messages (chat_id, sender, text, tg_message_id) VALUES (?, ?, ?, ?)',
                   (chat_id, sender, text, tg_message_id))
    conn.commit()
    conn.close()

def get_recent_messages(chat_id, limit=15):
    """Достает последние N сообщений для контекста."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT sender, text FROM messages
        WHERE chat_id = ? AND deleted = 0
        ORDER BY timestamp DESC LIMIT ?
    ''', (chat_id, limit))
    # Переворачиваем, чтобы старые были сверху
    messages = cursor.fetchall()[::-1]
    conn.close()
    return messages

def get_recent_messages_with_time(chat_id, limit=8):
    """Достает последние N сообщений вместе с timestamp (нужно для проверки плотности переписки)."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT sender, text, timestamp FROM messages
        WHERE chat_id = ? AND deleted = 0
        ORDER BY timestamp DESC LIMIT ?
    ''', (chat_id, limit))
    messages = cursor.fetchall()[::-1]
    conn.close()
    return messages

def update_message_text_by_tg_id(chat_id, tg_message_id, new_text):
    """Правит текст уже сохраненного сообщения при получении edited_business_message."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE messages SET text = ? WHERE chat_id = ? AND tg_message_id = ?
    ''', (new_text, chat_id, tg_message_id))
    conn.commit()
    conn.close()

def mark_message_deleted(chat_id, tg_message_id):
    """Помечает сообщение удаленным при получении deleted_business_messages."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE messages SET deleted = 1 WHERE chat_id = ? AND tg_message_id = ?
    ''', (chat_id, tg_message_id))
    conn.commit()
    conn.close()

def upsert_known_contact(chat_id, username, first_name):
    """Пассивно обновляет адресную книгу на каждое входящее сообщение (разрешен чат или нет).
    admin_notified не трогаем, чтобы не сбрасывать флаг уведомления при повторных сообщениях."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO known_contacts (chat_id, username, first_name, last_seen)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(chat_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_seen = excluded.last_seen
    ''', (chat_id, username, first_name))
    conn.commit()
    conn.close()

def get_known_contact(chat_id):
    """Возвращает (chat_id, username, first_name, last_seen, admin_notified) или None."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT chat_id, username, first_name, last_seen, admin_notified FROM known_contacts WHERE chat_id = ?', (chat_id,))
    result = cursor.fetchone()
    conn.close()
    return result

def mark_admin_notified(chat_id):
    """Помечает, что админ уже получил уведомление о новом чате (чтобы не слать повторно)."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE known_contacts SET admin_notified = 1 WHERE chat_id = ?', (chat_id,))
    conn.commit()
    conn.close()

def get_known_contacts(limit=8, offset=0, search=None):
    """Возвращает (chat_id, username, first_name) из адресной книги, новые сверху."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    if search:
        like = f"%{search}%"
        cursor.execute('''
            SELECT chat_id, username, first_name FROM known_contacts
            WHERE username LIKE ? OR first_name LIKE ?
            ORDER BY last_seen DESC LIMIT ? OFFSET ?
        ''', (like, like, limit, offset))
    else:
        cursor.execute('''
            SELECT chat_id, username, first_name FROM known_contacts
            ORDER BY last_seen DESC LIMIT ? OFFSET ?
        ''', (limit, offset))
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_known_contacts_count(search=None):
    """Считает количество записей в адресной книге (для пагинации)."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    if search:
        like = f"%{search}%"
        cursor.execute('SELECT COUNT(*) FROM known_contacts WHERE username LIKE ? OR first_name LIKE ?', (like, like))
    else:
        cursor.execute('SELECT COUNT(*) FROM known_contacts')
    count = cursor.fetchone()[0]
    conn.close()
    return count

def get_allowed_contacts(limit=8, offset=0, search=None):
    """Возвращает разрешенные чаты (profiles) вместе с именем/юзернеймом из адресной книги."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    if search:
        like = f"%{search}%"
        cursor.execute('''
            SELECT p.chat_id, kc.username, kc.first_name
            FROM profiles p
            LEFT JOIN known_contacts kc ON kc.chat_id = p.chat_id
            WHERE kc.username LIKE ? OR kc.first_name LIKE ?
            ORDER BY kc.last_seen DESC LIMIT ? OFFSET ?
        ''', (like, like, limit, offset))
    else:
        cursor.execute('''
            SELECT p.chat_id, kc.username, kc.first_name
            FROM profiles p
            LEFT JOIN known_contacts kc ON kc.chat_id = p.chat_id
            ORDER BY kc.last_seen DESC LIMIT ? OFFSET ?
        ''', (limit, offset))
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_allowed_contacts_count(search=None):
    """Считает количество разрешенных чатов (для пагинации)."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    if search:
        like = f"%{search}%"
        cursor.execute('''
            SELECT COUNT(*) FROM profiles p
            LEFT JOIN known_contacts kc ON kc.chat_id = p.chat_id
            WHERE kc.username LIKE ? OR kc.first_name LIKE ?
        ''', (like, like))
    else:
        cursor.execute('SELECT COUNT(*) FROM profiles')
    count = cursor.fetchone()[0]
    conn.close()
    return count

def get_unallowed_contacts(limit=8, offset=0):
    """Возвращает известные, но еще не разрешенные чаты (для мультивыбора 'Добавить чат')."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT kc.chat_id, kc.username, kc.first_name
        FROM known_contacts kc
        LEFT JOIN profiles p ON p.chat_id = kc.chat_id
        WHERE p.chat_id IS NULL
        ORDER BY kc.last_seen DESC LIMIT ? OFFSET ?
    ''', (limit, offset))
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_unallowed_contacts_count():
    """Считает количество известных, но еще не разрешенных чатов (для пагинации)."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT COUNT(*) FROM known_contacts kc
        LEFT JOIN profiles p ON p.chat_id = kc.chat_id
        WHERE p.chat_id IS NULL
    ''')
    count = cursor.fetchone()[0]
    conn.close()
    return count

def log_activity(chat_id, event):
    """Пишет событие в лог действий по чату (активность/статус/и т.д. — для панели админа)."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    cursor.execute('INSERT INTO activity_log (chat_id, event) VALUES (?, ?)', (chat_id, event))
    conn.commit()
    conn.close()

def get_activity_log(chat_id, limit=20):
    """Достает последние N событий лога для чата, в хронологическом порядке (старые сверху)."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT event, timestamp FROM activity_log
        WHERE chat_id = ? ORDER BY id DESC LIMIT ?
    ''', (chat_id, limit))
    rows = cursor.fetchall()[::-1]
    conn.close()
    return rows

def update_profile_summary(chat_id, summary):
    """Обновляет сводку профиля для данного чата, не затирая остальные поля профиля
    (раньше был INSERT OR REPLACE, который на каждый вызов стирал status и все прочие колонки)."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO profiles (chat_id, summary) VALUES (?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET summary = excluded.summary
    ''', (chat_id, summary))
    conn.commit()
    conn.close()

def get_profile_summary(chat_id):
    """Получает сводку профиля для данного чата."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT summary FROM profiles WHERE chat_id = ?', (chat_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def update_profile_status(chat_id, status):
    """Обновляет статус готовности клиента для данного чата."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE profiles SET status = ? WHERE chat_id = ?', (status, chat_id))
    conn.commit()
    conn.close()

def get_profile_status(chat_id):
    """Получает статус готовности клиента для данного чата."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT status FROM profiles WHERE chat_id = ?', (chat_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else 'новый' # Возвращаем 'новый' по умолчанию, если профиля нет

def update_profile_fields(chat_id, name=None, age=None, location=None, occupation=None,
                           financial_status=None, pain_points=None, personal_facts=None,
                           next_suggested_topic=None):
    """Обновляет расширенный структурированный профиль клиента (долгосрочная память),
    соответствует JSON-схеме из summary_prompt.txt / TOOLS['update_profile_data']."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE profiles SET
            name = ?, age = ?, location = ?, occupation = ?, financial_status = ?,
            pain_points = ?, personal_facts = ?, next_suggested_topic = ?
        WHERE chat_id = ?
    ''', (
        name, age, location, occupation, financial_status,
        json.dumps(pain_points, ensure_ascii=False) if pain_points is not None else None,
        json.dumps(personal_facts, ensure_ascii=False) if personal_facts is not None else None,
        next_suggested_topic, chat_id
    ))
    conn.commit()
    conn.close()

def get_profile_fields(chat_id):
    """Возвращает расширенный профиль клиента (summary/status + структурированные поля) как dict,
    или None если для этого чата еще нет записи в profiles."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT summary, status, name, age, location, occupation, financial_status,
               pain_points, personal_facts, next_suggested_topic
        FROM profiles WHERE chat_id = ?
    ''', (chat_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "summary": row[0],
        "status": row[1],
        "name": row[2],
        "age": row[3],
        "location": row[4],
        "occupation": row[5],
        "financial_status": row[6],
        "pain_points": json.loads(row[7]) if row[7] else [],
        "personal_facts": json.loads(row[8]) if row[8] else [],
        "next_suggested_topic": row[9],
    }


def add_allowed_chat(chat_id):
    """Добавляет chat_id в список разрешенных чатов, если его там нет."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    try:
        # Вставляем chat_id в таблицу profiles. 
        # summary пока оставляем пустым, но его можно использовать для заметок о чате.
        cursor.execute('INSERT INTO profiles (chat_id, summary) VALUES (?, ?)'
                       , (chat_id, ""))
        conn.commit()
        print(f"Чат {chat_id} добавлен в список разрешенных.")
    except sqlite3.IntegrityError:  # Если chat_id уже существует
        print(f"Чат {chat_id} уже есть в списке разрешенных.")
    conn.close()

def remove_allowed_chat(chat_id):
    """Удаляет chat_id из списка разрешенных чатов."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    cursor.execute('DELETE FROM profiles WHERE chat_id = ?', (chat_id,))
    conn.commit()
    print(f"Чат {chat_id} удален из списка разрешенных.")
    conn.close()

def is_chat_allowed(chat_id):
    """Проверяет, является ли чат разрешенным."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM profiles WHERE chat_id = ?', (chat_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def get_all_allowed_chats():
    """Возвращает список всех разрешенных chat_id."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    cursor.execute('SELECT chat_id, summary FROM profiles')
    chats = cursor.fetchall()
    conn.close()
    return chats


def save_biz_conn_id(chat_id: int, biz_conn_id: str):
    """Сохраняет ID бизнес-соединения для чата, чтобы бот мог писать первым от лица аккаунта."""
    if not biz_conn_id:
        return
        
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    
    # Создаем таблицу, если ее еще нет
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS biz_connections (
            chat_id INTEGER PRIMARY KEY,
            conn_id TEXT
        )
    ''')
    
    # Записываем или обновляем ключ для чата
    cursor.execute('''
        INSERT OR REPLACE INTO biz_connections (chat_id, conn_id)
        VALUES (?, ?)
    ''', (chat_id, biz_conn_id))
    
    conn.commit()
    conn.close()

def get_biz_conn_id(chat_id: int):
    """Достает ID бизнес-соединения для чата."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    
    try:
        cursor.execute('SELECT conn_id FROM biz_connections WHERE chat_id = ?', (chat_id,))
        row = cursor.fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        # Если таблица еще не создана
        return None
    finally:
        conn.close()