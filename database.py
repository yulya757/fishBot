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

def get_recent_messages(chat_id, limit=8):
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

def update_profile_summary(chat_id, summary):
    """Обновляет сводку профиля для данного чата."""
    conn = sqlite3.connect('memory.db')
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO profiles (chat_id, summary) VALUES (?, ?)', (chat_id, summary))
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