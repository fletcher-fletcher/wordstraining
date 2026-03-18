import telebot
import random
import sqlite3
import os
import threading
import io
import time
from dotenv import load_dotenv
from words_data import words_database
from gtts import gTTS
from threading import Lock
import logging
from requests.exceptions import ReadTimeout, ConnectionError
from flask import Flask
import schedule
from datetime import datetime

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Загружаем токен
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')

if not BOT_TOKEN:
    logger.error("BOT_TOKEN не найден в .env файле!")
    exit(1)

# СОЗДАЕМ БОТА
bot = telebot.TeleBot(BOT_TOKEN)
bot.timeout = 30

# Хранилище состояний пользователей
user_states = {}

# Хранилище обработанных callback'ов (для защиты от спама)
processed_callbacks = {}

# Блокировка для gTTS
tts_lock = Lock()

# ----- ФУНКЦИЯ ДЛЯ ПОЛУЧЕНИЯ ID ПОЛЬЗОВАТЕЛЯ -----
def get_user_id(obj):
    """
    УНИВЕРСАЛЬНОЕ получение ID пользователя.
    Работает и с Message, и с CallbackQuery.
    """
    if hasattr(obj, 'from_user') and obj.from_user:
        return obj.from_user.id
    elif hasattr(obj, 'message') and obj.message and obj.message.from_user:
        if hasattr(obj, 'from_user') and obj.from_user:
            return obj.from_user.id
    logger.warning(f"Не удалось определить ID пользователя из объекта: {type(obj)}")
    return None

# ----- ИНИЦИАЛИЗАЦИЯ FLASK (ДЛЯ RENDER) -----
app = Flask(__name__)

@app.route('/')
@app.route('/health')
def health():
    return "Bot is running", 200

# ----- РАБОТА С БАЗОЙ ДАННЫХ -----
def init_database():
    """Создает таблицы, если их нет, и заполняет словами"""
    conn = sqlite3.connect('words.db')
    cursor = conn.cursor()

    # Таблица для слов
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word TEXT UNIQUE,
            translation TEXT,
            example TEXT,
            example_translation TEXT,
            synonyms TEXT,
            part_of_speech TEXT
        )
    ''')

    # Таблица для пользователей и их сохраненных слов
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_words (
            user_id INTEGER,
            word_id INTEGER,
            added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notes TEXT DEFAULT '',
            PRIMARY KEY (user_id, word_id)
        )
    ''')

    # Таблица для уведомлений (история отправленных слов)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS notifications (
            user_id INTEGER,
            word_id INTEGER,
            sent_date DATE,
            UNIQUE(user_id, word_id, sent_date)
        )
    ''')
    
    # Таблица для настроек пользователей
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            notifications INTEGER DEFAULT 0,
            notify_time TEXT DEFAULT '10:00,15:00,20:00',
            last_notification DATE
        )
    ''')

    # Индексы для быстрого поиска
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_words_user_id ON user_words(user_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_words_word ON words(word)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_notifications_user_date ON notifications(user_id, sent_date)')

    # Заполняем словами из words_database
    for word_data in words_database:
        try:
            cursor.execute('''
                INSERT OR IGNORE INTO words
                (word, translation, example, example_translation, synonyms, part_of_speech)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                word_data['word'],
                word_data['translation'],
                word_data['example'],
                word_data['example_translation'],
                word_data['synonyms'],
                word_data['part_of_speech']
            ))
        except Exception as e:
            print(f"Ошибка при добавлении слова {word_data['word']}: {e}")

    conn.commit()
    conn.close()
    logger.info("База данных инициализирована")

def get_random_word(exclude_id=None):
    """Возвращает случайное слово из базы"""
    conn = sqlite3.connect('words.db')
    cursor = conn.cursor()

    if exclude_id:
        cursor.execute('SELECT * FROM words WHERE id != ? ORDER BY RANDOM() LIMIT 1', (exclude_id,))
    else:
        cursor.execute('SELECT * FROM words ORDER BY RANDOM() LIMIT 1')

    word = cursor.fetchone()
    conn.close()

    if word:
        return {
            'id': word[0],
            'word': word[1],
            'translation': word[2],
            'example': word[3],
            'example_translation': word[4],
            'synonyms': word[5],
            'part_of_speech': word[6]
        }
    return None

def get_unseen_word(user_id):
    """Возвращает случайное слово, которое ещё не показывали сегодня"""
    conn = sqlite3.connect('words.db')
    cursor = conn.cursor()
    
    today = time.strftime('%Y-%m-%d')
    
    # Слова, которые уже показывали сегодня
    cursor.execute('''
        SELECT word_id FROM notifications 
        WHERE user_id = ? AND sent_date = ?
    ''', (user_id, today))
    seen_today = [row[0] for row in cursor.fetchall()]
    
    # Если показали все слова, сбрасываем
    cursor.execute('SELECT COUNT(*) FROM words')
    total_words = cursor.fetchone()[0]
    
    if len(seen_today) >= total_words:
        cursor.execute('DELETE FROM notifications WHERE user_id = ? AND sent_date = ?', 
                      (user_id, today))
        seen_today = []
    
    # Получаем случайное слово из непоказанных
    if seen_today:
        placeholders = ','.join(['?'] * len(seen_today))
        cursor.execute(f'''
            SELECT * FROM words 
            WHERE id NOT IN ({placeholders})
            ORDER BY RANDOM() LIMIT 1
        ''', seen_today)
    else:
        cursor.execute('SELECT * FROM words ORDER BY RANDOM() LIMIT 1')
    
    word_data = cursor.fetchone()
    
    if word_data:
        word = {
            'id': word_data[0],
            'word': word_data[1],
            'translation': word_data[2],
            'example': word_data[3],
            'example_translation': word_data[4],
            'synonyms': word_data[5],
            'part_of_speech': word_data[6]
        }
        
        cursor.execute('''
            INSERT INTO notifications (user_id, word_id, sent_date)
            VALUES (?, ?, ?)
        ''', (user_id, word['id'], today))
        conn.commit()
    else:
        word = None
    
    conn.close()
    return word

def save_user_word(user_id, word_id, notes=""):
    """Сохраняет слово в список пользователя"""
    conn = sqlite3.connect('words.db')
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT * FROM user_words WHERE user_id = ? AND word_id = ?', 
                      (user_id, word_id))
        existing = cursor.fetchone()
        
        if existing:
            cursor.execute('SELECT COUNT(*) FROM user_words WHERE user_id = ?', (user_id,))
            count = cursor.fetchone()[0]
            conn.close()
            return count
        
        cursor.execute('''
            INSERT INTO user_words (user_id, word_id, notes)
            VALUES (?, ?, ?)
        ''', (user_id, word_id, notes))
        
        conn.commit()
        
        cursor.execute('SELECT COUNT(*) FROM user_words WHERE user_id = ?', (user_id,))
        count = cursor.fetchone()[0]
        conn.close()
        return count
    except Exception as e:
        print(f"Ошибка сохранения: {e}")
        conn.close()
        return None

def get_user_words(user_id):
    """Возвращает список сохраненных слов пользователя"""
    conn = sqlite3.connect('words.db')
    cursor = conn.cursor()

    cursor.execute('SELECT word_id FROM user_words WHERE user_id = ? ORDER BY added_date DESC', (user_id,))
    word_ids = cursor.fetchall()
    
    if not word_ids:
        conn.close()
        return []

    words = []
    for (word_id,) in word_ids:
        cursor.execute('SELECT * FROM words WHERE id = ?', (word_id,))
        word_data = cursor.fetchone()
        if word_data:
            words.append({
                'id': word_data[0],
                'word': word_data[1],
                'translation': word_data[2],
                'example': word_data[3],
                'example_translation': word_data[4],
                'synonyms': word_data[5],
                'part_of_speech': word_data[6],
                'notes': ""
            })
    
    conn.close()
    return words

def count_user_words(user_id):
    """Считает количество сохраненных слов у пользователя"""
    conn = sqlite3.connect('words.db')
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM user_words WHERE user_id = ?', (user_id,))
    count = cursor.fetchone()[0]
    conn.close()
    return count

# ----- ОЗВУЧКА ЧЕРЕЗ GTTS -----
def generate_voice(word):
    """Генерирует голосовое сообщение с произношением слова"""
    try:
        with tts_lock:
            tts = gTTS(text=word, lang='en', slow=False)
            audio_bytes = io.BytesIO()
            tts.write_to_fp(audio_bytes)
            audio_bytes.seek(0)
            return audio_bytes
    except Exception as e:
        print(f"Ошибка генерации голоса: {e}")
        return None

# ----- ФОРМАТИРОВАНИЕ СООБЩЕНИЙ -----
def format_word_card(word, user_id=None):
    """Форматирует слово в красивую карточку (без статистики)"""
    pos_emoji = {
        "adjective": "📘",
        "noun": "📗",
        "verb": "📙",
        "adverb": "📕"
    }
    emoji = pos_emoji.get(word['part_of_speech'], "📖")

    pos = f"{emoji} *{word['part_of_speech'].capitalize()}*" if word['part_of_speech'] else ""

    card = f"📖 *{word['word']}*\n"
    if pos:
        card += f"{pos}\n"
    card += f"📝 *Перевод:* {word['translation']}\n\n"
    card += f"📌 *Пример:*\n"
    card += f"{word['example']}\n"
    card += f"_{word['example_translation']}_\n\n"
    card += f"🔗 *Синонимы:* {word['synonyms']}"

    return card

# ----- УНИВЕРСАЛЬНАЯ ФУНКЦИЯ ДЛЯ КЛАВИАТУР -----
def get_unified_keyboard(word_id=None, mode="random", is_saved=False):
    """
    Создает унифицированную клавиатуру для всех случаев.
    
    Параметры:
    - word_id: ID слова (для кнопок сохранения/озвучки)
    - mode: "random" | "practice" | "search"
    - is_saved: True если слово уже сохранено
    """
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    buttons = []
    
    if word_id and not is_saved:
        buttons.append(telebot.types.InlineKeyboardButton("📥 Сохранить", callback_data=f"save_{word_id}"))
    elif word_id and is_saved:
        buttons.append(telebot.types.InlineKeyboardButton("✅ В списке", callback_data="noop"))
    
    if word_id:
        buttons.append(telebot.types.InlineKeyboardButton("🔊 Слушать", callback_data=f"voice_{word_id}"))
    
    if mode == "random":
        buttons.append(telebot.types.InlineKeyboardButton("🎲 Еще слово", callback_data="random"))
    elif mode == "practice":
        buttons.append(telebot.types.InlineKeyboardButton("🎯 Следующее", callback_data="continue_practice"))
    
    buttons.append(telebot.types.InlineKeyboardButton("📚 Мои слова", callback_data="show_mylist"))
    buttons.append(telebot.types.InlineKeyboardButton("🏠 Меню", callback_data="go_home"))
    
    for i in range(0, len(buttons), 2):
        if i + 1 < len(buttons):
            markup.add(buttons[i], buttons[i+1])
        else:
            markup.add(buttons[i])
    
    return markup

def get_main_menu_keyboard():
    """Создает клавиатуру главного меню"""
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        telebot.types.InlineKeyboardButton("🎲 Случайное слово", callback_data="menu_random"),
        telebot.types.InlineKeyboardButton("🎯 Тренировка", callback_data="menu_practice"),
        telebot.types.InlineKeyboardButton("📚 Мои слова", callback_data="show_mylist"),
        telebot.types.InlineKeyboardButton("🔔 Уведомления", callback_data="menu_notify"),
        telebot.types.InlineKeyboardButton("📝 Экзамен", callback_data="menu_exam")
    )
    return markup

# ----- ФУНКЦИИ ДЛЯ УВЕДОМЛЕНИЙ -----
def should_notify_now(user_id, current_time):
    """Проверяет, нужно ли отправить уведомление сейчас (по точному времени)"""
    conn = sqlite3.connect('words.db')
    cursor = conn.cursor()
    cursor.execute('SELECT notify_time FROM user_settings WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    
    if result and result[0]:
        times = result[0].split(',')
        # Сравниваем точное время ЧЧ:ММ
        return current_time in [t.strip() for t in times]
    return False

def send_scheduled_words():
    """Отправляет слова всем пользователям по расписанию"""
    conn = sqlite3.connect('words.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT user_id FROM user_settings WHERE notifications = 1')
    users = cursor.fetchall()
    conn.close()
    
    # Текущее время с минутами!
    current_time = datetime.now().strftime('%H:%M')
    
    print(f"⏰ Проверка уведомлений в {current_time}")  # Отладка
    
    for (user_id,) in users:
        try:
            if should_notify_now(user_id, current_time):
                print(f"📨 Отправка пользователю {user_id} в {current_time}")
                word = get_unseen_word(user_id)
                if word:
                    card = format_word_card(word, user_id=user_id)
                    markup = get_unified_keyboard(
                        word_id=word['id'],
                        mode="random",
                        is_saved=False
                    )
                    bot.send_message(
                        user_id, 
                        f"🔔 *Слово дня*\n\n{card}", 
                        parse_mode='Markdown',
                        reply_markup=markup
                    )
                    
                    conn = sqlite3.connect('words.db')
                    cursor = conn.cursor()
                    cursor.execute('''
                        UPDATE user_settings 
                        SET last_notification = date('now') 
                        WHERE user_id = ?
                    ''', (user_id,))
                    conn.commit()
                    conn.close()
        except Exception as e:
            print(f"❌ Ошибка отправки пользователю {user_id}: {e}")

def check_and_send():
    """Проверяет текущее время и отправляет уведомления (каждую минуту)"""
    # Убираем проверку :00 - отправляем всегда
    threading.Thread(target=send_scheduled_words).start()

def start_scheduler():
    """Запускает планировщик уведомлений"""
    schedule.every(1).minutes.do(check_and_send)
    while True:
        schedule.run_pending()
        time.sleep(1)

# ----- ФУНКЦИИ ДЛЯ ЭКЗАМЕНА -----
def send_exam_question(chat_id, user_id):
    """Отправляет следующий вопрос экзамена"""
    session = user_states.get(f"exam_{user_id}")
    
    if not session or session['current'] >= session['total']:
        finish_exam(chat_id, user_id)
        return
    
    word = session['words'][session['current']]
    
    conn = sqlite3.connect('words.db')
    cursor = conn.cursor()
    cursor.execute('SELECT translation FROM words WHERE id != ? ORDER BY RANDOM() LIMIT 3', (word['id'],))
    wrong_options = [row[0] for row in cursor.fetchall()]
    conn.close()
    
    options = [word['translation']] + wrong_options
    random.shuffle(options)
    
    question = f"❓ *Вопрос {session['current'] + 1}/{session['total']}*\n\n"
    question += f"Как переводится слово:\n*{word['word']}*"
    
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    
    for opt in options:
        is_correct = (opt == word['translation'])
        markup.add(telebot.types.InlineKeyboardButton(
            f"🔸 {opt}", 
            callback_data=f"exam_answer_{word['id']}_{is_correct}"
        ))
    
    bot.send_message(chat_id, question, parse_mode='Markdown', reply_markup=markup)

def finish_exam(chat_id, user_id):
    """Завершает экзамен и показывает результаты"""
    session = user_states.get(f"exam_{user_id}")
    
    if not session:
        return
    
    total = session['total']
    correct = session['correct']
    wrong = session['wrong']
    percentage = (correct / total * 100) if total > 0 else 0
    time_spent = int(time.time() - session['start_time'])
    
    result = f"📊 *Результаты экзамена*\n\n"
    result += f"✅ Правильно: {correct}\n"
    result += f"❌ Неправильно: {wrong}\n"
    result += f"🎯 Точность: {percentage:.1f}%\n"
    result += f"⏱ Время: {time_spent // 60} мин {time_spent % 60} сек\n\n"
    
    if percentage >= 90:
        result += "🏆 Отлично! Ты мастер слов!"
    elif percentage >= 70:
        result += "👍 Хорошо! Есть куда расти."
    elif percentage >= 50:
        result += "👌 Неплохо, но нужно повторить."
    else:
        result += "📚 Стоит ещё поучить эти слова."
    
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton("🔄 Ещё экзамен", callback_data="exam_again"),
        telebot.types.InlineKeyboardButton("📚 Мои слова", callback_data="show_mylist"),
        telebot.types.InlineKeyboardButton("🏠 Меню", callback_data="go_home")
    )
    
    bot.send_message(chat_id, result, parse_mode='Markdown', reply_markup=markup)
    
    if f"exam_{user_id}" in user_states:
        del user_states[f"exam_{user_id}"]

# ----- ОБРАБОТЧИКИ КОМАНД -----
@bot.message_handler(commands=['start'])
def start_command(message):
    """Приветственное сообщение"""
    user_id = get_user_id(message)
    if user_id:
        user_states[user_id] = {}
    
    welcome_text = """
👋 *Привет! Я бот для изучения умных английских слов.*

📝 *Просто напиши любое слово* — я найду его в словаре и покажу перевод, примеры и синонимы!

*Команды:*
/random — случайное слово
/practice — тренировка
/mylist — мои сохраненные слова
/notify — настроить уведомления
/exam — проверить знания
/menu — главное меню
    """
    show_main_menu(message.chat.id, welcome_text)

@bot.message_handler(commands=['menu'])
def menu_command(message):
    """Показать главное меню"""
    show_main_menu(message.chat.id)

def show_main_menu(chat_id, text="🏠 *Главное меню*"):
    markup = get_main_menu_keyboard()
    bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=markup)

@bot.message_handler(commands=['random'])
def random_word_command(message):
    user_id = get_user_id(message)
    if not user_id:
        bot.send_message(message.chat.id, "😕 Не удалось определить пользователя")
        return
        
    user_states[user_id] = {"mode": "random"}
    send_random_word(message.chat.id, user_id)

def send_random_word(chat_id, user_id):
    word = get_random_word()
    if word:
        conn = sqlite3.connect('words.db')
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM user_words WHERE user_id = ? AND word_id = ?', (user_id, word['id']))
        is_saved = cursor.fetchone() is not None
        conn.close()
        
        card = format_word_card(word, user_id=user_id)
        markup = get_unified_keyboard(
            word_id=word['id'],
            mode="random",
            is_saved=is_saved
        )
        bot.send_message(chat_id, card, parse_mode='Markdown', reply_markup=markup)
    else:
        bot.send_message(chat_id, "😕 Что-то пошло не так. Попробуй позже.")

@bot.message_handler(commands=['mylist'])
def mylist_command(message):
    """Показать список сохраненных слов"""
    user_id = get_user_id(message)
    if not user_id:
        bot.send_message(message.chat.id, "😕 Не удалось определить пользователя")
        return
        
    show_words_list(chat_id=message.chat.id, user_id=user_id)

def show_words_list(chat_id, user_id, edit_message_id=None):
    """Отображает список сохраненных слов"""
    words = get_user_words(user_id)

    if not words:
        text = "📭 У тебя пока нет сохраненных слов. Используй /random и сохраняй интересные!"
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("🏠 Меню", callback_data="go_home"))
        
        if edit_message_id:
            bot.edit_message_text(text, chat_id, edit_message_id, parse_mode='Markdown', reply_markup=markup)
        else:
            bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=markup)
        return

    text = "📚 *Твои сохраненные слова:*\n\n"
    for i, w in enumerate(words, 1):
        pos_symbol = "📘" if w['part_of_speech'] == "adjective" else "📗" if w['part_of_speech'] == "noun" else "📙"
        text += f"{i}. {pos_symbol} *{w['word']}* — {w['translation']}\n"

    text += f"\n📊 Всего: *{len(words)}* слов"

    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("🏠 Меню", callback_data="go_home"))

    if edit_message_id:
        bot.edit_message_text(text, chat_id, edit_message_id, parse_mode='Markdown', reply_markup=markup)
    else:
        bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=markup)

@bot.message_handler(commands=['practice'])
def practice_choice(message):
    """Выбор режима тренировки"""
    user_id = get_user_id(message)
    if not user_id:
        bot.send_message(message.chat.id, "😕 Не удалось определить пользователя")
        return
        
    user_states[user_id] = user_states.get(user_id, {})
    saved_count = count_user_words(user_id)
    
    print(f"🎯 practice_choice: user_id={user_id}, saved_count={saved_count}")

    markup = telebot.types.InlineKeyboardMarkup(row_width=2)

    btn_all = telebot.types.InlineKeyboardButton("🌍 По всем словам", callback_data="practice_mode_all")

    if saved_count > 0:
        btn_mylist = telebot.types.InlineKeyboardButton(f"📚 По моим словам ({saved_count})", callback_data="practice_mode_mylist")
        markup.add(btn_all, btn_mylist)
    else:
        markup.add(btn_all)

    markup.add(telebot.types.InlineKeyboardButton("🏠 Меню", callback_data="go_home"))

    bot.send_message(message.chat.id, "🎯 *Выбери режим тренировки*", parse_mode='Markdown', reply_markup=markup)

@bot.message_handler(commands=['notify'])
def notify_command(message):
    """Настройка уведомлений с отображением текущего статуса"""
    user_id = get_user_id(message)
    
    conn = sqlite3.connect('words.db')
    cursor = conn.cursor()
    cursor.execute('SELECT notifications, notify_time FROM user_settings WHERE user_id = ?', (user_id,))
    settings = cursor.fetchone()
    conn.close()
    
    status = "❌ Выключены"
    times = "10:00, 15:00, 20:00"
    
    if settings:
        if settings[0] == 1:
            status = "✅ Включены"
        times = settings[1].replace(',', ', ')
    
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        telebot.types.InlineKeyboardButton("✅ Включить" if status == "❌ Выключены" else "🔄 Перезапустить", 
                                          callback_data="notify_on"),
        telebot.types.InlineKeyboardButton("❌ Выключить", callback_data="notify_off"),
        telebot.types.InlineKeyboardButton("⏰ Установить время", callback_data="notify_set_time"),
        telebot.types.InlineKeyboardButton("📋 Мои настройки", callback_data="notify_show"),
        telebot.types.InlineKeyboardButton("🏠 Меню", callback_data="go_home")
    )
    
    status_text = f"""
🔔 *Настройка уведомлений*

📊 *Текущий статус:* {status}
⏱ *Время отправки:* {times}

Уведомления приходят каждый день в указанное время.
Каждый раз новое слово, которое ты ещё не видел сегодня.
Когда все слова заканчиваются - цикл повторяется.
"""
    
    bot.send_message(
        message.chat.id,
        status_text,
        parse_mode='Markdown',
        reply_markup=markup
    )

@bot.message_handler(commands=['exam'])
def exam_command(message):
    """Начинает режим экзамена"""
    user_id = get_user_id(message)
    
    saved_words = get_user_words(user_id)
    
    if len(saved_words) < 5:
        bot.send_message(
            message.chat.id,
            "📭 Для экзамена нужно минимум 5 сохранённых слов.\n"
            "Сохраняй слова через /random и возвращайся!"
        )
        return
    
    exam_session = {
        'words': saved_words.copy(),
        'current': 0,
        'correct': 0,
        'wrong': 0,
        'answers': [],
        'start_time': time.time()
    }
    
    random.shuffle(exam_session['words'])
    exam_session['words'] = exam_session['words'][:10]
    exam_session['total'] = len(exam_session['words'])
    
    user_states[f"exam_{user_id}"] = exam_session
    
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("🎯 Начать экзамен", callback_data="exam_start"))
    markup.add(telebot.types.InlineKeyboardButton("🏠 Меню", callback_data="go_home"))
    
    bot.send_message(
        message.chat.id,
        f"📝 *Экзамен*\n\n"
        f"Всего слов: {exam_session['total']}\n"
        f"Вопросы: перевод слова\n"
        f"Время: не ограничено\n\n"
        f"Готов начать?",
        parse_mode='Markdown',
        reply_markup=markup
    )

def start_practice_session(user_id, mode, chat_id):
    """Начинает сессию тренировки"""
    user_states[user_id] = {"mode": mode, "in_session": True, "last_word_id": None}

    if mode == "practice_all":
        word = get_random_word()
    else:
        user_words = get_user_words(user_id)
        if not user_words:
            bot.send_message(chat_id, "📭 У тебя пока нет слов для тренировки. Сохрани слова через /random")
            return
        
        if len(user_words) == 1 and user_states.get(user_id, {}).get("last_word_id") == user_words[0]['id']:
            markup = telebot.types.InlineKeyboardMarkup()
            markup.add(
                telebot.types.InlineKeyboardButton("🎲 Случайное слово", callback_data="random"),
                telebot.types.InlineKeyboardButton("🔄 Ещё раз", callback_data="practice_mode_mylist"),
                telebot.types.InlineKeyboardButton("🏠 Меню", callback_data="go_home")
            )
            bot.send_message(chat_id, "📭 *Ты повторил все слова!*", parse_mode='Markdown', reply_markup=markup)
            return
            
        word = random.choice(user_words)
        
        last_id = user_states.get(user_id, {}).get("last_word_id")
        if last_id and word['id'] == last_id and len(user_words) > 1:
            other_words = [w for w in user_words if w['id'] != last_id]
            word = random.choice(other_words)

    if not word:
        bot.send_message(chat_id, "😕 Не могу найти слово для тренировки. Попробуй позже.")
        return

    user_states[user_id]["last_word_id"] = word['id']

    conn = sqlite3.connect('words.db')
    cursor = conn.cursor()
    cursor.execute('SELECT translation FROM words WHERE id != ? ORDER BY RANDOM() LIMIT 3', (word['id'],))
    wrong_options = [row[0] for row in cursor.fetchall()]
    conn.close()

    options = [word['translation']] + wrong_options
    random.shuffle(options)

    mode_text = "из твоего списка" if mode == "practice_mylist" else "из словаря"
    question = f"❓ *Как переводится слово ({mode_text}):*\n*{word['word']}*"

    markup = telebot.types.InlineKeyboardMarkup(row_width=1)

    for opt in options:
        btn = telebot.types.InlineKeyboardButton(f"🔸 {opt}", callback_data=f"practice_answer_{word['id']}_{opt == word['translation']}")
        markup.add(btn)

    markup.add(
        telebot.types.InlineKeyboardButton("👀 Показать ответ", callback_data=f"practice_show_{word['id']}"),
        telebot.types.InlineKeyboardButton("🔊 Слушать", callback_data=f"voice_{word['id']}"),
        telebot.types.InlineKeyboardButton("🏠 Меню", callback_data="go_home")
    )

    bot.send_message(chat_id, question, parse_mode='Markdown', reply_markup=markup)

@bot.message_handler(func=lambda message: message.text and user_states.get(f"notify_time_{get_user_id(message)}", {}).get("step") == "waiting")
def handle_time_input(message):
    """Обрабатывает ввод времени пользователем для уведомлений"""
    user_id = get_user_id(message)
    chat_id = message.chat.id
    
    print(f"⏰ Получен ввод времени от {user_id}: {message.text}")  # Отладка
    
    time_text = message.text.strip()
    
    # Удаляем состояние
    if f"notify_time_{user_id}" in user_states:
        del user_states[f"notify_time_{user_id}"]
    
    # Валидация
    time_parts = time_text.split(',')
    valid_times = []
    invalid_times = []
    
    for part in time_parts:
        t = part.strip()
        try:
            if len(t) == 5 and t[2] == ':':
                hour = int(t[0:2])
                minute = int(t[3:5])
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    valid_times.append(t)
                else:
                    invalid_times.append(t)
            else:
                invalid_times.append(t)
        except:
            invalid_times.append(t)
    
    if not valid_times:
        bot.reply_to(
            message,
            f"❌ Неправильный формат времени: '{time_text}'\n\n"
            "Используй формат ЧЧ:ММ, например: 09:00, 15:30, 20:00\n"
            "Попробуй ещё раз через /notify"
        )
        return
    
    if invalid_times:
        warning = f"⚠️ Некоторые значения пропущены (неверный формат): {', '.join(invalid_times)}\n\n"
    else:
        warning = ""
    
    time_string = ', '.join(valid_times)
    
    conn = sqlite3.connect('words.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT INTO user_settings (user_id, notifications, notify_time)
        VALUES (?, 1, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            notifications = 1,
            notify_time = ?
    ''', (user_id, time_string, time_string))
    
    conn.commit()
    conn.close()
    
    success_text = f"""
✅ *Время сохранено!*

{warning}📅 Твои уведомления будут приходить в:
{', '.join(valid_times)}

🔔 Статус: *Включены*

Используй /notify для просмотра настроек
"""
    
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("🔔 Посмотреть настройки", callback_data="notify_show"))
    markup.add(telebot.types.InlineKeyboardButton("🏠 Меню", callback_data="go_home"))
    
    bot.reply_to(message, success_text, parse_mode='Markdown', reply_markup=markup)

# ----- ОБРАБОТЧИКИ КНОПОК -----
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    callback_id = f"{call.from_user.id}_{call.message.message_id}_{call.data}"
    
    if call.data.startswith("practice_answer_") and "False" in call.data:
        pass
    elif callback_id in processed_callbacks:
        bot.answer_callback_query(call.id, "⏳ Уже обрабатывается...")
        return
    else:
        processed_callbacks[callback_id] = time.time()
    
    current_time = time.time()
    expired = [k for k, v in processed_callbacks.items() if current_time - v > 10]
    for k in expired:
        del processed_callbacks[k]
    
    user_id = get_user_id(call)
    if not user_id:
        bot.answer_callback_query(call.id, "😕 Ошибка идентификации")
        return
        
    chat_id = call.message.chat.id
    message_id = call.message.message_id

    if not (call.data.startswith("practice_answer_") and "False" in call.data):
        bot.answer_callback_query(call.id, "⏳ Обрабатываю...")

    # ===== НАВИГАЦИЯ =====
    if call.data == "go_home":
        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass
        show_main_menu(chat_id)
        return
    
    if call.data == "noop":
        bot.answer_callback_query(call.id)
        return

    # ===== ГЛАВНОЕ МЕНЮ =====
    if call.data == "menu_random":
        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass
        user_states[user_id] = {"mode": "random"}
        send_random_word(chat_id, user_id)
        return
    
    if call.data == "menu_practice":
        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass
        class SimpleMessage:
            def __init__(self, chat_id, user_id, msg_id=0):
                self.chat = type('obj', (object,), {'id': chat_id})
                self.from_user = type('obj', (object,), {'id': user_id})
                self.message_id = msg_id
        
        fake_msg = SimpleMessage(chat_id, user_id, message_id)
        practice_choice(fake_msg)
        return
    
    if call.data == "menu_notify":
        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass
        class SimpleMessage:
            def __init__(self, chat_id, user_id, msg_id=0):
                self.chat = type('obj', (object,), {'id': chat_id})
                self.from_user = type('obj', (object,), {'id': user_id})
                self.message_id = msg_id
        
        fake_msg = SimpleMessage(chat_id, user_id, message_id)
        notify_command(fake_msg)
        return
    
    if call.data == "menu_exam":
        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass
        class SimpleMessage:
            def __init__(self, chat_id, user_id, msg_id=0):
                self.chat = type('obj', (object,), {'id': chat_id})
                self.from_user = type('obj', (object,), {'id': user_id})
                self.message_id = msg_id
        
        fake_msg = SimpleMessage(chat_id, user_id, message_id)
        exam_command(fake_msg)
        return

    # ===== УВЕДОМЛЕНИЯ =====
    if call.data == "notify_on":
        conn = sqlite3.connect('words.db')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO user_settings (user_id, notifications, notify_time)
            VALUES (?, 1, '10:00,15:00,20:00')
            ON CONFLICT(user_id) DO UPDATE SET
                notifications = 1
        ''', (user_id,))
        conn.commit()
        conn.close()
        
        bot.answer_callback_query(call.id, "✅ Уведомления включены!")
        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass
        show_main_menu(chat_id)
        return
    
    if call.data == "notify_off":
        conn = sqlite3.connect('words.db')
        cursor = conn.cursor()
        cursor.execute('UPDATE user_settings SET notifications = 0 WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()
        
        bot.answer_callback_query(call.id, "✅ Уведомления выключены")
        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass
        show_main_menu(chat_id)
        return
    
    if call.data == "notify_show":
        conn = sqlite3.connect('words.db')
        cursor = conn.cursor()
        cursor.execute('SELECT notifications, notify_time, last_notification FROM user_settings WHERE user_id = ?', (user_id,))
        settings = cursor.fetchone()
        
        today = time.strftime('%Y-%m-%d')
        cursor.execute('SELECT COUNT(*) FROM notifications WHERE user_id = ? AND sent_date = ?', (user_id, today))
        sent_today = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM words')
        total_words = cursor.fetchone()[0]
        conn.close()
        
        if not settings:
            bot.answer_callback_query(call.id, "Настройки не найдены")
            return
        
        status = "✅ Включены" if settings[0] == 1 else "❌ Выключены"
        times = settings[1].replace(',', ', ')
        last = settings[2] if settings[2] else "никогда"
        
        detail_text = f"""
📋 *Детальные настройки*

🔔 *Статус:* {status}
⏰ *Время отправки:* {times}
📅 *Последнее уведомление:* {last}
📊 *Отправлено сегодня:* {sent_today} слов
📚 *Всего слов в словаре:* {total_words}

Каждый день ты получаешь новые слова, пока не увидишь все.
После этого цикл повторяется заново.
"""
        
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("🔙 Назад", callback_data="notify_back"))
        
        try:
            bot.edit_message_text(detail_text, chat_id, message_id, parse_mode='Markdown', reply_markup=markup)
        except:
            bot.send_message(chat_id, detail_text, parse_mode='Markdown', reply_markup=markup)
        
        bot.answer_callback_query(call.id)
        return
    
    if call.data == "notify_set_time":
        # Устанавливаем состояние ожидания ввода времени
        user_states[f"notify_time_{user_id}"] = {"step": "waiting"}
        
        conn = sqlite3.connect('words.db')
        cursor = conn.cursor()
        cursor.execute('SELECT notify_time FROM user_settings WHERE user_id = ?', (user_id,))
        settings = cursor.fetchone()
        conn.close()
        
        current_times = settings[0] if settings else "10:00, 15:00, 20:00"
        
        instruction_text = f"""
    ⏰ *Настройка времени уведомлений*

    📝 *Текущее время:* {current_times.replace(',', ', ')}

    *Как настроить:*
    1. Введи время в формате ЧЧ:ММ
    2. Можно указать несколько через запятую
    3. Например: `09:00, 14:30, 20:00`

    ⚠️ *Важно:* время указывай в 24-часовом формате
    📱 Примеры:
    • `08:00` - одно уведомление в день
    • `10:00, 18:00` - два уведомления
    • `09:30, 14:00, 20:30` - три уведомления

    Отправь время в чат или нажми Отмена
    """
        
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("❌ Отмена", callback_data="notify_back"))
        
        try:
            bot.edit_message_text(instruction_text, chat_id, message_id, parse_mode='Markdown', reply_markup=markup)
        except:
            bot.send_message(chat_id, instruction_text, parse_mode='Markdown', reply_markup=markup)
        
        bot.answer_callback_query(call.id)
        return
    
    if call.data == "notify_back":
        if f"notify_time_{user_id}" in user_states:
            del user_states[f"notify_time_{user_id}"]
        
        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass
        
        class SimpleMessage:
            def __init__(self, chat_id, user_id):
                self.chat = type('obj', (object,), {'id': chat_id})
                self.from_user = type('obj', (object,), {'id': user_id})
        
        fake_msg = SimpleMessage(chat_id, user_id)
        notify_command(fake_msg)
        return

    # ===== ЭКЗАМЕН =====
    if call.data == "exam_start":
        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass
        send_exam_question(chat_id, user_id)
        return
    
    if call.data.startswith("exam_answer_"):
        parts = call.data.split("_")
        word_id = int(parts[2])
        is_correct = parts[3] == "True"
        
        session = user_states.get(f"exam_{user_id}")
        
        if session:
            if is_correct:
                session['correct'] += 1
            else:
                session['wrong'] += 1
            
            session['current'] += 1
            
            try:
                bot.delete_message(chat_id, message_id)
            except:
                pass
            
            if session['current'] >= session['total']:
                finish_exam(chat_id, user_id)
            else:
                send_exam_question(chat_id, user_id)
        return
    
    if call.data == "exam_again":
        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass
        class SimpleMessage:
            def __init__(self, chat_id, user_id, msg_id=0):
                self.chat = type('obj', (object,), {'id': chat_id})
                self.from_user = type('obj', (object,), {'id': user_id})
                self.message_id = msg_id
        
        fake_msg = SimpleMessage(chat_id, user_id, message_id)
        exam_command(fake_msg)
        return

    # ===== СПИСОК СЛОВ =====
    if call.data == "show_mylist":
        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass
        show_words_list(chat_id, user_id)
        return

    # ===== ОЗВУЧКА =====
    if call.data.startswith("voice_"):
        word_id = int(call.data.split("_")[1])
        
        conn = sqlite3.connect('words.db')
        cursor = conn.cursor()
        cursor.execute('SELECT word FROM words WHERE id = ?', (word_id,))
        word_text = cursor.fetchone()[0]
        conn.close()

        audio_bytes = generate_voice(word_text)
        if audio_bytes:
            bot.send_voice(chat_id, audio_bytes, caption=f"Произношение: {word_text}")
        else:
            bot.send_message(chat_id, "😕 Не удалось сгенерировать произношение.")
        return

    # ===== ТРЕНИРОВКА =====
    if call.data == "practice_mode_all":
        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass
        start_practice_session(user_id, "practice_all", chat_id)
        return
    
    if call.data == "practice_mode_mylist":
        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass
        start_practice_session(user_id, "practice_mylist", chat_id)
        return

    # ===== СЛУЧАЙНОЕ СЛОВО =====
    if call.data == "random":
        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass
        user_states[user_id] = {"mode": "random"}
        send_random_word(chat_id, user_id)
        return

    # ===== СОХРАНЕНИЕ =====
    if call.data.startswith("save_"):
        word_id = int(call.data.split("_")[1])
        count = save_user_word(user_id, word_id)

        if count:
            bot.answer_callback_query(call.id, f"✅ Сохранено! Теперь {count} слов.")
        else:
            bot.answer_callback_query(call.id, "✅ Сохранено!")

        conn = sqlite3.connect('words.db')
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM words WHERE id = ?', (word_id,))
        word_data = cursor.fetchone()
        conn.close()
        
        if word_data:
            word = {
                'id': word_data[0],
                'word': word_data[1],
                'translation': word_data[2],
                'example': word_data[3],
                'example_translation': word_data[4],
                'synonyms': word_data[5],
                'part_of_speech': word_data[6]
            }
            card = format_word_card(word, user_id=user_id)
            markup = get_unified_keyboard(
                word_id=word_id,
                mode="random",
                is_saved=True
            )
            
            try:
                bot.edit_message_text(card, chat_id, message_id, parse_mode='Markdown', reply_markup=markup)
            except:
                bot.send_message(chat_id, card, parse_mode='Markdown', reply_markup=markup)
        return

    # ===== ПРОДОЛЖЕНИЕ ТРЕНИРОВКИ =====
    if call.data == "continue_practice":
        mode = user_states.get(user_id, {}).get("mode")
        
        if not mode:
            try:
                bot.delete_message(chat_id, message_id)
            except:
                pass
            class SimpleMessage:
                def __init__(self, chat_id, user_id, msg_id=0):
                    self.chat = type('obj', (object,), {'id': chat_id})
                    self.from_user = type('obj', (object,), {'id': user_id})
                    self.message_id = msg_id
            fake_msg = SimpleMessage(chat_id, user_id, message_id)
            practice_choice(fake_msg)
            return
        
        if mode == "practice_all":
            word = get_random_word()
            if not word:
                bot.send_message(chat_id, "😕 Слова закончились.")
                return
        else:
            user_words = get_user_words(user_id)
            if not user_words:
                markup = telebot.types.InlineKeyboardMarkup()
                markup.add(
                    telebot.types.InlineKeyboardButton("🎲 Случайное слово", callback_data="random"),
                    telebot.types.InlineKeyboardButton("🏠 Меню", callback_data="go_home")
                )
                bot.send_message(chat_id, "📭 Нет слов для тренировки.", reply_markup=markup)
                return
            
            if len(user_words) == 1 and user_states.get(user_id, {}).get("last_word_id") == user_words[0]['id']:
                markup = telebot.types.InlineKeyboardMarkup()
                markup.add(
                    telebot.types.InlineKeyboardButton("🎲 Случайное слово", callback_data="random"),
                    telebot.types.InlineKeyboardButton("🔄 Ещё раз", callback_data="practice_mode_mylist"),
                    telebot.types.InlineKeyboardButton("🏠 Меню", callback_data="go_home")
                )
                bot.send_message(chat_id, "📭 *Ты повторил все слова!*", parse_mode='Markdown', reply_markup=markup)
                return
                
            word = random.choice(user_words)
            
            last_id = user_states.get(user_id, {}).get("last_word_id")
            if last_id and word['id'] == last_id and len(user_words) > 1:
                other_words = [w for w in user_words if w['id'] != last_id]
                word = random.choice(other_words)
        
        if not word:
            bot.send_message(chat_id, "😕 Не могу найти слово.")
            return
        
        user_states[user_id]["last_word_id"] = word['id']
        
        conn = sqlite3.connect('words.db')
        cursor = conn.cursor()
        cursor.execute('SELECT translation FROM words WHERE id != ? ORDER BY RANDOM() LIMIT 3', (word['id'],))
        wrong_options = [row[0] for row in cursor.fetchall()]
        conn.close()
        
        while len(wrong_options) < 3:
            wrong_options.append("???")
        
        options = [word['translation']] + wrong_options
        random.shuffle(options)
        
        mode_text = "из твоего списка" if mode == "practice_mylist" else "из словаря"
        question = f"❓ *Как переводится слово ({mode_text}):*\n*{word['word']}*"
        
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        
        for opt in options:
            markup.add(telebot.types.InlineKeyboardButton(f"🔸 {opt}", callback_data=f"practice_answer_{word['id']}_{opt == word['translation']}"))
        
        markup.add(
            telebot.types.InlineKeyboardButton("👀 Показать ответ", callback_data=f"practice_show_{word['id']}"),
            telebot.types.InlineKeyboardButton("🔊 Слушать", callback_data=f"voice_{word['id']}"),
            telebot.types.InlineKeyboardButton("🏠 Меню", callback_data="go_home")
        )
        
        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass
        
        bot.send_message(chat_id, question, parse_mode='Markdown', reply_markup=markup)
        
        keys_to_delete = []
        for key in list(processed_callbacks.keys()):
            if key.startswith(f"{user_id}_"):
                keys_to_delete.append(key)
        
        for key in keys_to_delete:
            del processed_callbacks[key]
        
        return

    # ===== ОТВЕТЫ В ТРЕНИРОВКЕ =====
    if call.data.startswith("practice_answer_"):
        parts = call.data.split("_")
        word_id = int(parts[2])
        is_correct = parts[3] == "True"

        print(f"🔥 practice_answer_: user_id={user_id}, word_id={word_id}, is_correct={is_correct}")

        if not is_correct:
            bot.answer_callback_query(call.id, "❌ Неправильно! Попробуй другой вариант.", show_alert=True)
            return

        bot.answer_callback_query(call.id, "✅ Правильно!")

        conn = sqlite3.connect('words.db')
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM words WHERE id = ?', (word_id,))
        word_data = cursor.fetchone()
        conn.close()

        if word_data:
            word = {
                'id': word_data[0],
                'word': word_data[1],
                'translation': word_data[2],
                'example': word_data[3],
                'example_translation': word_data[4],
                'synonyms': word_data[5],
                'part_of_speech': word_data[6]
            }

            conn = sqlite3.connect('words.db')
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM user_words WHERE user_id = ? AND word_id = ?', (user_id, word_id))
            is_saved = cursor.fetchone() is not None
            conn.close()
            
            card = format_word_card(word, user_id=user_id)
            markup = get_unified_keyboard(
                word_id=word_id,
                mode="practice",
                is_saved=is_saved
            )

            try:
                bot.edit_message_text(f"✅ *Верно!*\n\n{card}", chat_id, message_id, parse_mode='Markdown', reply_markup=markup)
            except:
                bot.send_message(chat_id, f"✅ *Верно!*\n\n{card}", parse_mode='Markdown', reply_markup=markup)
        return

    # ===== ПОКАЗАТЬ ОТВЕТ =====
    if call.data.startswith("practice_show_"):
        word_id = int(call.data.split("_")[2])

        conn = sqlite3.connect('words.db')
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM words WHERE id = ?', (word_id,))
        word_data = cursor.fetchone()
        conn.close()

        if word_data:
            word = {
                'id': word_data[0],
                'word': word_data[1],
                'translation': word_data[2],
                'example': word_data[3],
                'example_translation': word_data[4],
                'synonyms': word_data[5],
                'part_of_speech': word_data[6]
            }

            bot.answer_callback_query(call.id, "👀 Вот правильный ответ!")

            conn = sqlite3.connect('words.db')
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM user_words WHERE user_id = ? AND word_id = ?', (user_id, word_id))
            is_saved = cursor.fetchone() is not None
            conn.close()
            
            card = format_word_card(word, user_id=user_id)
            markup = get_unified_keyboard(
                word_id=word_id,
                mode="practice",
                is_saved=is_saved
            )

            try:
                bot.edit_message_text(f"👀 *Правильный ответ:*\n\n{card}", chat_id, message_id, parse_mode='Markdown', reply_markup=markup)
            except:
                bot.send_message(chat_id, f"👀 *Правильный ответ:*\n\n{card}", parse_mode='Markdown', reply_markup=markup)
        return

@bot.message_handler(func=lambda m: True)
def handle_text(message):
    if message.text.startswith('/'):
        return
        
    word_text = message.text.strip().lower()

    conn = sqlite3.connect('words.db')
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM words WHERE LOWER(word) = ?', (word_text,))
    word_data = cursor.fetchone()
    conn.close()

    if word_data:
        user_id = get_user_id(message)
        if not user_id:
            bot.send_message(message.chat.id, "😕 Не удалось определить пользователя")
            return
            
        conn = sqlite3.connect('words.db')
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM user_words WHERE user_id = ? AND word_id = ?', (user_id, word_data[0]))
        is_saved = cursor.fetchone() is not None
        conn.close()
        
        word = {
            'id': word_data[0],
            'word': word_data[1],
            'translation': word_data[2],
            'example': word_data[3],
            'example_translation': word_data[4],
            'synonyms': word_data[5],
            'part_of_speech': word_data[6]
        }
        card = format_word_card(word, user_id=user_id)

        markup = get_unified_keyboard(
            word_id=word['id'],
            mode="search",
            is_saved=is_saved
        )

        bot.send_message(message.chat.id, card, parse_mode='Markdown', reply_markup=markup)
    else:
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("🏠 Меню", callback_data="go_home"))
        bot.send_message(message.chat.id, f"😕 Не знаю слова '{message.text}'. Попробуй другое или зайди в меню.", reply_markup=markup)
        
# ----- ЗАПУСК БОТА -----
def run_bot():
    """Запускает бота"""
    try:
        bot.infinity_polling(timeout=30, long_polling_timeout=20)
    except Exception as e:
        logger.error(f"Ошибка бота: {e}")
        time.sleep(5)

if __name__ == "__main__":
    print("Запускаем приложение...")
    try:
        init_database()
        print("✅ База данных готова!")

        try:
            test_audio = generate_voice("test")
            if test_audio:
                print("✅ gTTS работает")
        except Exception as e:
            print(f"⚠️ Ошибка при проверке gTTS: {e}")

        bot_thread = threading.Thread(target=run_bot, daemon=True)
        bot_thread.start()
        print("✅ Бот запущен")

        scheduler_thread = threading.Thread(target=start_scheduler, daemon=True)
        scheduler_thread.start()
        print("✅ Планировщик уведомлений запущен")

        port = int(os.environ.get('PORT', 10000))
        print(f"🚀 Запускаем Flask на порту {port}...")
        app.run(host='0.0.0.0', port=port, debug=True, use_reloader=False)

    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
