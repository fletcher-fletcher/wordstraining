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

bot = telebot.TeleBot(BOT_TOKEN)
bot.timeout = 30

# Хранилище состояний пользователей
user_states = {}

# Блокировка для gTTS
tts_lock = Lock()

# ----- ИНИЦИАЛИЗАЦИЯ FLASK (ДЛЯ RENDER) -----
app = Flask(__name__)

@app.route('/')
@app.route('/health')
def health():
    """Эндпоинт для проверки здоровья сервиса Render"""
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

    # Для старых баз - добавляем колонку notes если её нет
    try:
        cursor.execute('ALTER TABLE user_words ADD COLUMN notes TEXT DEFAULT ""')
    except sqlite3.OperationalError:
        pass

    # Таблица для статистики по словам
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS word_stats (
            user_id INTEGER,
            word_id INTEGER,
            correct INTEGER DEFAULT 0,
            wrong INTEGER DEFAULT 0,
            last_review TIMESTAMP,
            PRIMARY KEY (user_id, word_id)
        )
    ''')

    # Для старых баз - добавляем колонки в word_stats если их нет
    try:
        cursor.execute('ALTER TABLE word_stats ADD COLUMN correct INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute('ALTER TABLE word_stats ADD COLUMN wrong INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute('ALTER TABLE word_stats ADD COLUMN last_review TIMESTAMP')
    except sqlite3.OperationalError:
        pass

    # Индексы для быстрого поиска
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_words_user_id ON user_words(user_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_words_word ON words(word)')

    # Заполняем словами из words_database, если их еще нет
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
    """Возвращает случайное слово из базы, можно исключить конкретное слово"""
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

def save_user_word(user_id, word_id, notes=""):
    """Сохраняет слово в список пользователя"""
    conn = sqlite3.connect('words.db')
    cursor = conn.cursor()
    try:
        cursor.execute('''
            INSERT OR IGNORE INTO user_words (user_id, word_id, notes)
            VALUES (?, ?, ?)
        ''', (user_id, word_id, notes))
        conn.commit()

        cursor.execute('SELECT COUNT(*) FROM user_words WHERE user_id = ?', (user_id,))
        count = cursor.fetchone()[0]
        return count
    except Exception as e:
        print(f"Ошибка сохранения: {e}")
        return None
    finally:
        conn.close()

def get_user_words(user_id):
    """Возвращает список сохраненных слов пользователя"""
    conn = sqlite3.connect('words.db')
    cursor = conn.cursor()

    # Сначала проверим, есть ли колонка notes
    cursor.execute("PRAGMA table_info(user_words)")
    columns = [col[1] for col in cursor.fetchall()]

    if 'notes' in columns:
        cursor.execute('''
            SELECT w.*, uw.notes FROM words w
            JOIN user_words uw ON w.id = uw.word_id
            WHERE uw.user_id = ?
            ORDER BY uw.added_date DESC
        ''', (user_id,))
    else:
        # Если нет notes, выбираем без неё
        cursor.execute('''
            SELECT w.* FROM words w
            JOIN user_words uw ON w.id = uw.word_id
            WHERE uw.user_id = ?
            ORDER BY uw.added_date DESC
        ''', (user_id,))

    words = cursor.fetchall()
    conn.close()

    result = []
    for w in words:
        if len(w) > 7:
            result.append({
                'id': w[0],
                'word': w[1],
                'translation': w[2],
                'example': w[3],
                'example_translation': w[4],
                'synonyms': w[5],
                'part_of_speech': w[6],
                'notes': w[7] if len(w) > 7 else ""
            })
        else:
            result.append({
                'id': w[0],
                'word': w[1],
                'translation': w[2],
                'example': w[3],
                'example_translation': w[4],
                'synonyms': w[5],
                'part_of_speech': w[6],
                'notes': ""
            })
    return result

def get_random_user_word(user_id, exclude_id=None):
    """Возвращает случайное слово из списка пользователя"""
    conn = sqlite3.connect('words.db')
    cursor = conn.cursor()

    if exclude_id:
        cursor.execute('''
            SELECT w.* FROM words w
            JOIN user_words uw ON w.id = uw.word_id
            WHERE uw.user_id = ? AND w.id != ?
            ORDER BY RANDOM() LIMIT 1
        ''', (user_id, exclude_id))
    else:
        cursor.execute('''
            SELECT w.* FROM words w
            JOIN user_words uw ON w.id = uw.word_id
            WHERE uw.user_id = ?
            ORDER BY RANDOM() LIMIT 1
        ''', (user_id,))

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

def count_user_words(user_id):
    """Считает количество сохраненных слов у пользователя"""
    conn = sqlite3.connect('words.db')
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM user_words WHERE user_id = ?', (user_id,))
    count = cursor.fetchone()[0]
    conn.close()
    return count

def update_word_stats(user_id, word_id, correct):
    """Обновляет статистику по слову"""
    conn = sqlite3.connect('words.db')
    cursor = conn.cursor()

    if correct:
        cursor.execute('''
            INSERT INTO word_stats (user_id, word_id, correct, wrong, last_review)
            VALUES (?, ?, 1, 0, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, word_id) DO UPDATE SET
                correct = correct + 1,
                last_review = CURRENT_TIMESTAMP
        ''', (user_id, word_id))
    else:
        cursor.execute('''
            INSERT INTO word_stats (user_id, word_id, correct, wrong, last_review)
            VALUES (?, ?, 0, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, word_id) DO UPDATE SET
                wrong = wrong + 1,
                last_review = CURRENT_TIMESTAMP
        ''', (user_id, word_id))

    conn.commit()
    conn.close()

def get_word_stats(user_id, word_id):
    """Возвращает статистику по слову"""
    conn = sqlite3.connect('words.db')
    cursor = conn.cursor()
    cursor.execute('SELECT correct, wrong FROM word_stats WHERE user_id = ? AND word_id = ?',
                  (user_id, word_id))
    stats = cursor.fetchone()
    conn.close()

    if stats:
        return {'correct': stats[0], 'wrong': stats[1]}
    return {'correct': 0, 'wrong': 0}

def add_note_to_word(user_id, word_id, note):
    """Добавляет заметку к слову"""
    conn = sqlite3.connect('words.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE user_words SET notes = ? WHERE user_id = ? AND word_id = ?',
                  (note, user_id, word_id))
    conn.commit()
    conn.close()

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
def format_word_card(word, show_stats=True, user_id=None):
    """Форматирует слово в красивую карточку"""
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

    if show_stats and user_id:
        stats = get_word_stats(user_id, word['id'])
        if stats['correct'] > 0 or stats['wrong'] > 0:
            total = stats['correct'] + stats['wrong']
            percent = (stats['correct'] / total * 100) if total > 0 else 0
            card += f"\n\n📊 *Статистика:* ✅ {stats['correct']} | ❌ {stats['wrong']} ({percent:.0f}%)"

    return card

def get_main_menu_keyboard():
    """Создает клавиатуру главного меню"""
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)

    btn1 = telebot.types.InlineKeyboardButton("🎲 Случайное слово", callback_data="menu_random")
    btn2 = telebot.types.InlineKeyboardButton("🎯 Тренировка", callback_data="menu_practice")
    btn3 = telebot.types.InlineKeyboardButton("📚 Мои слова", callback_data="menu_mylist")
    btn4 = telebot.types.InlineKeyboardButton("📊 Статистика", callback_data="menu_stats")
    btn5 = telebot.types.InlineKeyboardButton("❓ Помощь", callback_data="menu_help")

    markup.add(btn1, btn2, btn3, btn4, btn5)
    return markup

# ----- ОБРАБОТЧИКИ КОМАНД -----
@bot.message_handler(commands=['start', 'menu'])
def start_command(message):
    show_main_menu(message.chat.id, "👋 Добро пожаловать! Выбери действие:")

def show_main_menu(chat_id, text="🏠 *Главное меню*"):
    markup = get_main_menu_keyboard()
    bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=markup)

@bot.message_handler(commands=['help'])
def help_command(message):
    help_text = """
❓ *Помощь по командам*

🏠 *Главное меню* — все основные функции

🎲 *Случайное слово* — получить случайное слово с карточкой
🎯 *Тренировка* — проверить свои знания
📚 *Мои слова* — список сохраненных слов
📊 *Статистика* — твой прогресс

🔊 *Озвучка* — в карточке слова есть кнопка для прослушивания
📝 *Заметки* — можно добавлять свои примечания к словам

Просто напиши любое слово — я найду его в словаре!
    """
    bot.reply_to(message, help_text, parse_mode='Markdown')

@bot.message_handler(commands=['stats'])
def stats_command(message):
    user_id = message.from_user.id
    saved_count = count_user_words(user_id)

    conn = sqlite3.connect('words.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT SUM(correct), SUM(wrong) FROM word_stats WHERE user_id = ?
    ''', (user_id,))
    stats = cursor.fetchone()
    conn.close()

    total_correct = stats[0] or 0
    total_wrong = stats[1] or 0
    total_attempts = total_correct + total_wrong

    stats_text = f"📊 *Твоя статистика*\n\n"
    stats_text += f"📚 Сохранено слов: *{saved_count}*\n"
    stats_text += f"✅ Правильных ответов: *{total_correct}*\n"
    stats_text += f"❌ Неправильных: *{total_wrong}*\n"

    if total_attempts > 0:
        accuracy = (total_correct / total_attempts * 100)
        stats_text += f"🎯 Точность: *{accuracy:.1f}%*\n"

    markup = telebot.types.InlineKeyboardMarkup()
    home_btn = telebot.types.InlineKeyboardButton("🏠 Главное меню", callback_data="go_home")
    markup.add(home_btn)

    bot.send_message(message.chat.id, stats_text, parse_mode='Markdown', reply_markup=markup)

@bot.message_handler(commands=['random'])
def random_word_command(message):
    user_id = message.from_user.id
    user_states[user_id] = {"mode": "random"}
    send_random_word(message.chat.id, user_id)

def send_random_word(chat_id, user_id):
    word = get_random_word()
    if word:
        card = format_word_card(word, user_id=user_id)

        markup = telebot.types.InlineKeyboardMarkup(row_width=2)
        save_btn = telebot.types.InlineKeyboardButton("📥 Сохранить", callback_data=f"save_{word['id']}")
        voice_btn = telebot.types.InlineKeyboardButton("🔊 Произнести", callback_data=f"voice_{word['id']}")
        another_btn = telebot.types.InlineKeyboardButton("🎲 Еще", callback_data="random")
        markup.add(save_btn, voice_btn, another_btn)

        home_btn = telebot.types.InlineKeyboardButton("🏠 Меню", callback_data="go_home")
        markup.add(home_btn)

        bot.send_message(chat_id, card, parse_mode='Markdown', reply_markup=markup)
    else:
        bot.send_message(chat_id, "😕 Что-то пошло не так. Попробуй позже.")

@bot.message_handler(commands=['mylist'])
def mylist_command(message):
    user_id = message.from_user.id
    words = get_user_words(user_id)

    if not words:
        bot.reply_to(message, "📭 У тебя пока нет сохраненных слов. Используй /random и сохраняй интересные!")
        return

    show_words_page(message.chat.id, user_id, words, page=0)

def show_words_page(chat_id, user_id, words, page=0, edit_message_id=None):
    page_size = 10
    total_pages = (len(words) + page_size - 1) // page_size
    start = page * page_size
    end = min(start + page_size, len(words))

    text = f"📚 *Твои слова (страница {page + 1}/{total_pages}):*\n\n"

    for i, w in enumerate(words[start:end], start=start+1):
        pos_symbol = "📘" if w['part_of_speech'] == "adjective" else "📗" if w['part_of_speech'] == "noun" else "📙"
        text += f"{i}. {pos_symbol} *{w['word']}* — {w['translation']}\n"

    text += f"\n📊 Всего: *{len(words)}* слов"

    markup = telebot.types.InlineKeyboardMarkup(row_width=3)

    nav_btns = []
    if page > 0:
        nav_btns.append(telebot.types.InlineKeyboardButton("◀️", callback_data=f"words_page_{page-1}"))
    nav_btns.append(telebot.types.InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav_btns.append(telebot.types.InlineKeyboardButton("▶️", callback_data=f"words_page_{page+1}"))

    if nav_btns:
        markup.add(*nav_btns)

    practice_btn = telebot.types.InlineKeyboardButton("🎯 Тренироваться по своим словам", callback_data="practice_mode_mylist")
    markup.add(practice_btn)

    home_btn = telebot.types.InlineKeyboardButton("🏠 Главное меню", callback_data="go_home")
    markup.add(home_btn)

    if edit_message_id:
        bot.edit_message_text(text, chat_id, edit_message_id, parse_mode='Markdown', reply_markup=markup)
    else:
        bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=markup)

@bot.message_handler(commands=['practice'])
def practice_choice(message):
    user_id = message.from_user.id
    saved_count = count_user_words(user_id)

    markup = telebot.types.InlineKeyboardMarkup(row_width=2)

    btn_all = telebot.types.InlineKeyboardButton("🌍 По всем словам", callback_data="practice_mode_all")

    if saved_count > 0:
        btn_mylist = telebot.types.InlineKeyboardButton(f"📚 По моим словам ({saved_count})", callback_data="practice_mode_mylist")
        markup.add(btn_all, btn_mylist)
    else:
        markup.add(btn_all)

    home_btn = telebot.types.InlineKeyboardButton("🏠 Главное меню", callback_data="go_home")
    markup.add(home_btn)

    bot.send_message(message.chat.id, "🎯 *Выбери режим тренировки*", parse_mode='Markdown', reply_markup=markup)

def start_practice_session(user_id, mode, chat_id):
    user_states[user_id] = {"mode": mode, "in_session": True}

    if mode == "practice_all":
        word = get_random_word()
    else:
        word = get_random_user_word(user_id)

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

    show_btn = telebot.types.InlineKeyboardButton("👀 Показать ответ", callback_data=f"practice_show_{word['id']}")
    voice_btn = telebot.types.InlineKeyboardButton("🔊 Послушать", callback_data=f"voice_{word['id']}")
    markup.add(show_btn, voice_btn)

    home_btn = telebot.types.InlineKeyboardButton("🏠 Меню", callback_data="go_home")
    markup.add(home_btn)

    bot.send_message(chat_id, question, parse_mode='Markdown', reply_markup=markup)

# ----- ОБРАБОТЧИКИ ТЕКСТА (ПОИСК) -----
@bot.message_handler(func=lambda m: True)
def handle_text(message):
    word_text = message.text.strip().lower()

    conn = sqlite3.connect('words.db')
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM words WHERE LOWER(word) = ?', (word_text,))
    word_data = cursor.fetchone()
    conn.close()

    if word_data:
        user_id = message.from_user.id
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

        markup = telebot.types.InlineKeyboardMarkup(row_width=2)
        save_btn = telebot.types.InlineKeyboardButton("📥 Сохранить", callback_data=f"save_{word['id']}")
        voice_btn = telebot.types.InlineKeyboardButton("🔊 Произнести", callback_data=f"voice_{word['id']}")
        markup.add(save_btn, voice_btn)

        home_btn = telebot.types.InlineKeyboardButton("🏠 Меню", callback_data="go_home")
        markup.add(home_btn)

        bot.reply_to(message, card, parse_mode='Markdown', reply_markup=markup)
    else:
        markup = telebot.types.InlineKeyboardMarkup()
        home_btn = telebot.types.InlineKeyboardButton("🏠 Главное меню", callback_data="go_home")
        markup.add(home_btn)

        bot.reply_to(message, f"😕 Не знаю слова '{message.text}'. Попробуй другое или зайди в меню.", reply_markup=markup)

# ----- ОБРАБОТЧИКИ КНОПОК -----
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    message_id = call.message.message_id

    if call.data == "go_home":
        bot.delete_message(chat_id, message_id)
        show_main_menu(chat_id)
        return

    if call.data == "noop":
        bot.answer_callback_query(call.id)
        return

    if call.data == "menu_random":
        bot.delete_message(chat_id, message_id)
        send_random_word(chat_id, user_id)
        return

    if call.data == "menu_practice":
        bot.delete_message(chat_id, message_id)
        # Используем прямую отправку сообщения вместо FakeMessage
        sent_msg = bot.send_message(chat_id, "⚡ Загружаем тренировку...")
        practice_choice(sent_msg)
        return

    if call.data == "menu_mylist":
        bot.delete_message(chat_id, message_id)
        sent_msg = bot.send_message(chat_id, "📚 Загружаем ваш список...")
        mylist_command(sent_msg)
        return

    if call.data == "menu_stats":
        bot.delete_message(chat_id, message_id)
        sent_msg = bot.send_message(chat_id, "📊 Загружаем статистику...")
        stats_command(sent_msg)
        return

    if call.data == "menu_help":
        bot.delete_message(chat_id, message_id)
        sent_msg = bot.send_message(chat_id, "❓ Загружаем помощь...")
        help_command(sent_msg)
        return

    if call.data.startswith("words_page_"):
        page = int(call.data.split("_")[2])
        words = get_user_words(user_id)
        show_words_page(chat_id, user_id, words, page, message_id)
        bot.answer_callback_query(call.id)
        return

    if call.data.startswith("voice_"):
        word_id = int(call.data.split("_")[1])

        conn = sqlite3.connect('words.db')
        cursor = conn.cursor()
        cursor.execute('SELECT word FROM words WHERE id = ?', (word_id,))
        word_text = cursor.fetchone()[0]
        conn.close()

        bot.answer_callback_query(call.id, "🔊 Генерирую произношение...")

        audio_bytes = generate_voice(word_text)
        if audio_bytes:
            bot.send_voice(chat_id, audio_bytes, caption=f"Произношение: {word_text}")
        else:
            bot.send_message(chat_id, "😕 Не удалось сгенерировать произношение. Попробуй позже.")
        return

    if call.data == "practice_mode_all":
        bot.edit_message_text("🎯 Начинаем тренировку по *всем словам*!", chat_id, message_id, parse_mode='Markdown')
        start_practice_session(user_id, "practice_all", chat_id)
        return

    if call.data == "practice_mode_mylist":
        bot.edit_message_text("🎯 Начинаем тренировку по *твоим словам*!", chat_id, message_id, parse_mode='Markdown')
        start_practice_session(user_id, "practice_mylist", chat_id)
        return

    if call.data == "random":
        bot.delete_message(chat_id, message_id)
        user_states[user_id] = {"mode": "random"}
        send_random_word(chat_id, user_id)
        return

    if call.data.startswith("save_"):
        word_id = int(call.data.split("_")[1])

        count = save_user_word(user_id, word_id)

        if count:
            bot.answer_callback_query(call.id, f"✅ Слово сохранено! Теперь у тебя {count} слов.")
        else:
            bot.answer_callback_query(call.id, "✅ Слово сохранено!")

        markup = telebot.types.InlineKeyboardMarkup(row_width=2)

        if user_id in user_states and user_states[user_id].get("mode") in ["practice_all", "practice_mylist"]:
            next_btn = telebot.types.InlineKeyboardButton("🎯 Продолжить", callback_data="continue_practice")
        else:
            next_btn = telebot.types.InlineKeyboardButton("🎲 Еще слово", callback_data="random")

        voice_btn = telebot.types.InlineKeyboardButton("🔊 Послушать", callback_data=f"voice_{word_id}")
        mylist_btn = telebot.types.InlineKeyboardButton("📚 Мои слова", callback_data="menu_mylist")
        home_btn = telebot.types.InlineKeyboardButton("🏠 Меню", callback_data="go_home")

        markup.add(next_btn, voice_btn, mylist_btn, home_btn)

        bot.edit_message_reply_markup(chat_id, message_id, reply_markup=markup)
        return

    if call.data == "continue_practice":
        bot.delete_message(chat_id, message_id)

        if user_id not in user_states or user_states[user_id].get("mode") not in ["practice_all", "practice_mylist"]:
            sent_msg = bot.send_message(chat_id, "⚡ Возвращаемся к тренировке...")
            practice_choice(sent_msg)
            return

        mode = user_states[user_id]["mode"]
        last_word_id = user_states[user_id].get("last_word_id")

        if mode == "practice_all":
            word = get_random_word(exclude_id=last_word_id)
        else:
            word = get_random_user_word(user_id, exclude_id=last_word_id)

        if not word:
            if mode == "practice_mylist":
                word = get_random_user_word(user_id)
                if word:
                    bot.send_message(chat_id, "ℹ️ У тебя только одно слово в списке. Повторяем его.")
                else:
                    bot.send_message(chat_id, "📭 В твоем списке нет слов для тренировки.")
                    return
            else:
                word = get_random_word()

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

        show_btn = telebot.types.InlineKeyboardButton("👀 Показать ответ", callback_data=f"practice_show_{word['id']}")
        voice_btn = telebot.types.InlineKeyboardButton("🔊 Послушать", callback_data=f"voice_{word['id']}")
        markup.add(show_btn, voice_btn)

        home_btn = telebot.types.InlineKeyboardButton("🏠 Меню", callback_data="go_home")
        markup.add(home_btn)

        bot.send_message(chat_id, question, parse_mode='Markdown', reply_markup=markup)
        return

    if call.data.startswith("practice_answer_"):
        parts = call.data.split("_")
        word_id = int(parts[2])
        is_correct = parts[3] == "True"

        update_word_stats(user_id, word_id, is_correct)

        if is_correct:
            bot.answer_callback_query(call.id, "✅ Правильно! Молодец!")
        else:
            bot.answer_callback_query(call.id, "❌ Неправильно", show_alert=True)
            return

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

            markup = telebot.types.InlineKeyboardMarkup(row_width=2)
            save_btn = telebot.types.InlineKeyboardButton("📥 Сохранить", callback_data=f"save_{word_id}")
            voice_btn = telebot.types.InlineKeyboardButton("🔊 Послушать", callback_data=f"voice_{word_id}")
            continue_btn = telebot.types.InlineKeyboardButton("🎯 Далее", callback_data="continue_practice")
            home_btn = telebot.types.InlineKeyboardButton("🏠 Меню", callback_data="go_home")

            markup.add(save_btn, voice_btn, continue_btn, home_btn)

            bot.edit_message_text(f"✅ *Верно!*\n\n{card}", chat_id, message_id, parse_mode='Markdown', reply_markup=markup)
        return

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

            card = format_word_card(word, user_id=user_id)

            markup = telebot.types.InlineKeyboardMarkup(row_width=2)
            save_btn = telebot.types.InlineKeyboardButton("📥 Сохранить", callback_data=f"save_{word_id}")
            voice_btn = telebot.types.InlineKeyboardButton("🔊 Послушать", callback_data=f"voice_{word_id}")
            continue_btn = telebot.types.InlineKeyboardButton("🎯 Далее", callback_data="continue_practice")
            home_btn = telebot.types.InlineKeyboardButton("🏠 Меню", callback_data="go_home")

            markup.add(save_btn, voice_btn, continue_btn, home_btn)

            bot.edit_message_text(f"👀 *Правильный ответ:*\n\n{card}", chat_id, message_id, parse_mode='Markdown', reply_markup=markup)
        return

# ----- ФУНКЦИЯ ДЛЯ ЗАПУСКА БОТА В ПОТОКЕ -----
def run_bot():
    """Запускает бота в фоновом потоке"""
    print("Запускаем бота в фоновом потоке...")
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=20)
        except (ReadTimeout, ConnectionError) as e:
            logger.error(f"Ошибка подключения бота: {e}. Переподключение через 5 секунд...")
            time.sleep(5)
            continue
        except Exception as e:
            logger.error(f"Неожиданная ошибка бота: {e}")
            time.sleep(5)
            continue

# ----- ЗАПУСК ПРИЛОЖЕНИЯ -----
if __name__ == "__main__":
    print("Запускаем приложение...")
    try:
        init_database()
        print("✅ База данных готова!")

        try:
            test_audio = generate_voice("test")
            if test_audio:
                print("✅ gTTS работает")
            else:
                print("⚠️ gTTS не работает, озвучка будет недоступна")
        except Exception as e:
            print(f"⚠️ Ошибка при проверке gTTS: {e}")

        # Запускаем бота в фоновом потоке
        bot_thread = threading.Thread(target=run_bot, daemon=True)
        bot_thread.start()
        print("✅ Бот запущен в фоновом потоке")

        # Запускаем Flask в главном потоке (это то, что ждет Render)
        port = int(os.environ.get('PORT', 10000))
        print(f"🚀 Запускаем Flask на порту {port}...")
        app.run(host='0.0.0.0', port=port)

    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
