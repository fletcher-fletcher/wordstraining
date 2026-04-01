import telebot
import random
import os
import threading
import io
import time
import json
from dotenv import load_dotenv
from words_data import words_database
from gtts import gTTS
from threading import Lock
import logging
from requests.exceptions import ReadTimeout, ConnectionError
from flask import Flask
import schedule
from datetime import datetime
import pytz
import requests
import groq
from supabase import create_client, Client

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Загружаем токен
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
GROQ_API_KEY = os.getenv('GROQ_API_KEY')
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')

if not BOT_TOKEN:
    logger.error("BOT_TOKEN не найден в .env файле!")
    exit(1)

# Инициализация Supabase клиента
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

if not supabase:
    logger.error("SUPABASE_URL и SUPABASE_KEY не настроены!")
    exit(1)

# Инициализация GROQ клиента
groq_client = groq.Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

def get_word_from_ai(word):
    """Получает информацию о слове из GROQ AI (бесплатно и быстро)"""
    if not GROQ_API_KEY or not groq_client:
        print("❌ GROQ API ключ не настроен")
        return None
    
    prompt = f"""Ты помогаешь с английскими словами. Отвечай строго в формате JSON без лишнего текста.
Если слово не английское или не существует, верни {{"error": "unknown"}}.

Пример ответа для слова "hello":
{{
    "word": "hello",
    "translation": "привет",
    "example": "Hello, how are you?",
    "example_translation": "Привет, как дела?",
    "synonyms": "hi, greetings",
    "part_of_speech": "interjection"
}}

Теперь ответь для слова: {word}
"""
    
    try:
        print(f"🤖 Отправка запроса к GROQ для слова: {word}")
        
        completion = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "user", "content": prompt}
            ],
            temperature=0.5,
            max_tokens=500
        )
        
        content = completion.choices[0].message.content
        
        # Извлекаем JSON из ответа
        content = content.strip()
        if content.startswith('```json'):
            content = content[7:]
        if content.startswith('```'):
            content = content[3:]
        if content.endswith('```'):
            content = content[:-3]
        content = content.strip()
        
        ai_data = json.loads(content)
        
        if ai_data.get('error'):
            print(f"❌ AI вернул ошибку: {ai_data.get('error')}")
            return None
            
        return {
            'word': ai_data.get('word', word),
            'translation': ai_data.get('translation', ''),
            'example': ai_data.get('example', ''),
            'example_translation': ai_data.get('example_translation', ''),
            'synonyms': ai_data.get('synonyms', ''),
            'part_of_speech': ai_data.get('part_of_speech', 'unknown')
        }
    except Exception as e:
        print(f"❌ Ошибка GROQ: {e}")
        return None
        
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

# ----- РАБОТА С SUPABASE -----
def init_database():
    """Инициализирует таблицы в Supabase и заполняет словами"""
    try:
        # Проверяем, есть ли слова в таблице
        response = supabase.table('words').select('count', count='exact').execute()
        
        if response.count == 0:
            print("📚 Заполняем базу данных словами...")
            # Заполняем словами из words_database
            for word_data in words_database:
                try:
                    supabase.table('words').insert({
                        'word': word_data['word'],
                        'translation': word_data['translation'],
                        'example': word_data['example'],
                        'example_translation': word_data['example_translation'],
                        'synonyms': word_data['synonyms'],
                        'part_of_speech': word_data['part_of_speech']
                    }).execute()
                except Exception as e:
                    print(f"Ошибка при добавлении слова {word_data['word']}: {e}")
            print("✅ База данных заполнена!")
        else:
            print(f"✅ База данных уже содержит {response.count} слов")
            
        logger.info("База данных инициализирована")
    except Exception as e:
        logger.error(f"Ошибка инициализации базы данных: {e}")

def get_random_word(exclude_id=None):
    """Возвращает случайное слово из базы"""
    try:
        query = supabase.table('words').select('*')
        
        if exclude_id:
            query = query.neq('id', exclude_id)
        
        # Получаем количество слов
        count_response = supabase.table('words').select('count', count='exact').execute()
        total = count_response.count
        
        if total == 0:
            return None
        
        # Получаем случайное слово
        random_offset = random.randint(0, total - 1)
        response = query.range(random_offset, random_offset).execute()
        
        if response.data:
            word_data = response.data[0]
            return {
                'id': word_data['id'],
                'word': word_data['word'],
                'translation': word_data['translation'],
                'example': word_data['example'],
                'example_translation': word_data['example_translation'],
                'synonyms': word_data['synonyms'],
                'part_of_speech': word_data['part_of_speech']
            }
        return None
    except Exception as e:
        print(f"Ошибка получения случайного слова: {e}")
        return None

def get_unseen_word(user_id):
    """Возвращает случайное слово, которое ещё не показывали сегодня"""
    try:
        today = time.strftime('%Y-%m-%d')
        
        # Получаем слова, показанные сегодня
        seen_response = supabase.table('notifications')\
            .select('word_id')\
            .eq('user_id', user_id)\
            .eq('sent_date', today)\
            .execute()
        
        seen_today = [row['word_id'] for row in seen_response.data]
        
        # Получаем общее количество слов
        total_response = supabase.table('words').select('count', count='exact').execute()
        total_words = total_response.count
        
        # Если показали все слова, сбрасываем
        if len(seen_today) >= total_words:
            supabase.table('notifications')\
                .delete()\
                .eq('user_id', user_id)\
                .eq('sent_date', today)\
                .execute()
            seen_today = []
        
        # Получаем случайное слово из непоказанных
        query = supabase.table('words').select('*')
        
        if seen_today:
            query = query.neq('id', seen_today[0])
            for word_id in seen_today[1:]:
                query = query.neq('id', word_id)
        
        # Получаем количество доступных слов
        count_response = supabase.table('words').select('count', count='exact').execute()
        available_count = count_response.count - len(seen_today)
        
        if available_count > 0:
            random_offset = random.randint(0, available_count - 1)
            response = query.range(random_offset, random_offset).execute()
            
            if response.data:
                word_data = response.data[0]
                word = {
                    'id': word_data['id'],
                    'word': word_data['word'],
                    'translation': word_data['translation'],
                    'example': word_data['example'],
                    'example_translation': word_data['example_translation'],
                    'synonyms': word_data['synonyms'],
                    'part_of_speech': word_data['part_of_speech']
                }
                
                # Сохраняем в уведомления
                supabase.table('notifications').insert({
                    'user_id': user_id,
                    'word_id': word['id'],
                    'sent_date': today
                }).execute()
                
                return word
        
        return None
    except Exception as e:
        print(f"Ошибка получения непоказанного слова: {e}")
        return None

def save_user_word(user_id, word_id, notes=""):
    """Сохраняет слово в список пользователя"""
    try:
        # Проверяем, есть ли уже такое слово
        existing = supabase.table('user_words')\
            .select('*')\
            .eq('user_id', user_id)\
            .eq('word_id', word_id)\
            .execute()
        
        if existing.data:
            # Считаем количество слов
            count_response = supabase.table('user_words')\
                .select('count', count='exact')\
                .eq('user_id', user_id)\
                .execute()
            return count_response.count
        
        # Сохраняем новое слово
        supabase.table('user_words').insert({
            'user_id': user_id,
            'word_id': word_id,
            'notes': notes,
            'added_date': datetime.now().isoformat()
        }).execute()
        
        # Считаем количество слов
        count_response = supabase.table('user_words')\
            .select('count', count='exact')\
            .eq('user_id', user_id)\
            .execute()
        
        return count_response.count
    except Exception as e:
        print(f"Ошибка сохранения: {e}")
        return None

def get_user_words(user_id):
    """Возвращает список сохраненных слов пользователя"""
    try:
        # Получаем ID слов пользователя
        user_words_response = supabase.table('user_words')\
            .select('word_id')\
            .eq('user_id', user_id)\
            .order('added_date', desc=True)\
            .execute()
        
        if not user_words_response.data:
            return []
        
        word_ids = [row['word_id'] for row in user_words_response.data]
        
        # Получаем слова по ID
        words = []
        for word_id in word_ids:
            word_response = supabase.table('words')\
                .select('*')\
                .eq('id', word_id)\
                .execute()
            
            if word_response.data:
                word_data = word_response.data[0]
                words.append({
                    'id': word_data['id'],
                    'word': word_data['word'],
                    'translation': word_data['translation'],
                    'example': word_data['example'],
                    'example_translation': word_data['example_translation'],
                    'synonyms': word_data['synonyms'],
                    'part_of_speech': word_data['part_of_speech'],
                    'notes': ""
                })
        
        return words
    except Exception as e:
        print(f"Ошибка получения слов пользователя: {e}")
        return []

def count_user_words(user_id):
    """Считает количество сохраненных слов у пользователя"""
    try:
        response = supabase.table('user_words')\
            .select('count', count='exact')\
            .eq('user_id', user_id)\
            .execute()
        
        return response.count
    except Exception as e:
        print(f"Ошибка подсчета слов: {e}")
        return 0

def get_user_settings(user_id):
    """Получает настройки пользователя"""
    try:
        response = supabase.table('user_settings')\
            .select('*')\
            .eq('user_id', user_id)\
            .execute()
        
        if response.data:
            return response.data[0]
        return None
    except Exception as e:
        print(f"Ошибка получения настроек: {e}")
        return None

def update_user_settings(user_id, settings):
    """Обновляет настройки пользователя"""
    try:
        # Проверяем, есть ли настройки
        existing = supabase.table('user_settings')\
            .select('*')\
            .eq('user_id', user_id)\
            .execute()
        
        if existing.data:
            # Обновляем существующие
            supabase.table('user_settings')\
                .update(settings)\
                .eq('user_id', user_id)\
                .execute()
        else:
            # Создаем новые
            supabase.table('user_settings').insert({
                'user_id': user_id,
                **settings
            }).execute()
        
        return True
    except Exception as e:
        print(f"Ошибка обновления настроек: {e}")
        return False

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
    if word['synonyms']:
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
    settings = get_user_settings(user_id)
    
    if settings and settings.get('notify_time'):
        times = settings['notify_time'].split(',')
        return current_time in [t.strip() for t in times]
    return False

def send_scheduled_words():
    """Отправляет слова всем пользователям с учётом их часового пояса"""
    try:
        # Получаем всех пользователей с включенными уведомлениями
        response = supabase.table('user_settings')\
            .select('user_id, notify_time, timezone')\
            .eq('notifications', 1)\
            .execute()
        
        users = response.data
        server_time = datetime.now(pytz.UTC)
        
        for user_data in users:
            user_id = user_data['user_id']
            notify_time = user_data.get('notify_time')
            tz_name = user_data.get('timezone', 'UTC')
            
            try:
                if tz_name and tz_name != 'UTC':
                    tz = pytz.timezone(tz_name)
                    user_time = server_time.astimezone(tz).strftime('%H:%M')
                else:
                    user_time = server_time.strftime('%H:%M')
                
                if notify_time and user_time in [t.strip() for t in notify_time.split(',')]:
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
                        
                        # Обновляем время последнего уведомления
                        supabase.table('user_settings')\
                            .update({'last_notification': datetime.now().date().isoformat()})\
                            .eq('user_id', user_id)\
                            .execute()
            except Exception as e:
                print(f"❌ Ошибка для {user_id}: {e}")
    except Exception as e:
        print(f"❌ Ошибка в send_scheduled_words: {e}")

def check_and_send():
    """Проверяет текущее время и отправляет уведомления (каждую минуту)"""
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
    
    try:
        # Получаем случайные варианты ответов
        response = supabase.table('words')\
            .select('translation')\
            .neq('id', word['id'])\
            .limit(3)\
            .execute()
        
        wrong_options = [row['translation'] for row in response.data]
        
        # Если не хватает вариантов, добавляем заглушки
        while len(wrong_options) < 3:
            wrong_options.append("???")
        
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
    except Exception as e:
        print(f"Ошибка в send_exam_question: {e}")

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

@bot.message_handler(commands=['test_ai'])
def test_ai_command(message):
    """Тест API GROQ"""
    status_msg = bot.reply_to(message, "🔍 Тестирую подключение к GROQ...")
    
    test_word = "hello"
    result = get_word_from_ai(test_word)
    
    if result:
        bot.edit_message_text(
            f"✅ AI работает!\n\nСлово: {result['word']}\nПеревод: {result['translation']}",
            message.chat.id,
            status_msg.message_id
        )
    else:
        bot.edit_message_text(
            "❌ AI не отвечает. Проверь API ключ.",
            message.chat.id,
            status_msg.message_id
        )
        
def show_notify_settings(chat_id, user_id, edit_message_id=None):
    """Показывает настройки уведомлений"""
    settings = get_user_settings(user_id)
    
    status = "❌ Выключены"
    times = "10:00, 15:00, 20:00"
    tz = "UTC"
    
    if settings:
        if settings.get('notifications') == 1:
            status = "✅ Включены"
        times = settings.get('notify_time', "10:00,15:00,20:00").replace(',', ', ')
        tz = settings.get('timezone', "UTC")
    
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        telebot.types.InlineKeyboardButton("✅ Включить" if status == "❌ Выключены" else "🔄 Включены", 
                                          callback_data="notify_on"),
        telebot.types.InlineKeyboardButton("❌ Выключить", callback_data="notify_off"),
        telebot.types.InlineKeyboardButton("⏰ Установить время", callback_data="notify_set_time"),
        telebot.types.InlineKeyboardButton("🌍 Часовой пояс", callback_data="notify_timezone"),
        telebot.types.InlineKeyboardButton("🏠 Меню", callback_data="go_home")
    )
    
    status_text = f"""
🔔 *Настройка уведомлений*

📊 *Текущий статус:* {status}
⏱ *Время отправки:* {times}
🌍 *Часовой пояс:* {tz}

Уведомления приходят каждый день в указанное время.
Каждый раз новое слово, которое ты ещё не видел сегодня.
Когда все слова заканчиваются - цикл повторяется.
"""
    
    if edit_message_id:
        try:
            bot.edit_message_text(status_text, chat_id, edit_message_id, parse_mode='Markdown', reply_markup=markup)
        except:
            bot.send_message(chat_id, status_text, parse_mode='Markdown', reply_markup=markup)
    else:
        bot.send_message(chat_id, status_text, parse_mode='Markdown', reply_markup=markup)

@bot.message_handler(commands=['timezone'])
def timezone_command(message):
    """Настройка часового пояса"""
    user_id = get_user_id(message)
    
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        telebot.types.InlineKeyboardButton("🇷🇺 Москва (MSK, UTC+3)", callback_data="tz_Europe/Moscow"),
        telebot.types.InlineKeyboardButton("🇬🇧 Лондон (UTC)", callback_data="tz_Europe/London"),
        telebot.types.InlineKeyboardButton("🇪🇺 Берлин (CET, UTC+1)", callback_data="tz_Europe/Berlin"),
        telebot.types.InlineKeyboardButton("🇺🇸 Нью-Йорк (EST, UTC-5)", callback_data="tz_America/New_York"),
        telebot.types.InlineKeyboardButton("🔙 Назад", callback_data="notify_back")
    )
    
    bot.send_message(
        message.chat.id,
        "🌍 *Настройка часового пояса*\n\n"
        "Выбери свой часовой пояс, чтобы уведомления приходили в правильное время.\n"
        "Сейчас у тебя UTC (Лондон), поэтому уведомления приходят на 3 часа позже.",
        parse_mode='Markdown',
        reply_markup=markup
    )
    
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
        # Проверяем, сохранено ли слово
        existing = supabase.table('user_words')\
            .select('*')\
            .eq('user_id', user_id)\
            .eq('word_id', word['id'])\
            .execute()
        
        is_saved = len(existing.data) > 0
        
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
    """Настройка уведомлений"""
    user_id = get_user_id(message)
    show_notify_settings(message.chat.id, user_id)

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

    try:
        # Получаем случайные варианты ответов
        response = supabase.table('words')\
            .select('translation')\
            .neq('id', word['id'])\
            .limit(3)\
            .execute()
        
        wrong_options = [row['translation'] for row in response.data]
        
        # Если не хватает вариантов, добавляем заглушки
        while len(wrong_options) < 3:
            wrong_options.append("???")
        
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
    except Exception as e:
        print(f"Ошибка в start_practice_session: {e}")

@bot.message_handler(func=lambda message: message.text and user_states.get(f"notify_time_{get_user_id(message)}", {}).get("step") == "waiting")
def handle_time_input(message):
    """Обрабатывает ввод времени пользователем для уведомлений"""
    user_id = get_user_id(message)
    chat_id = message.chat.id
    
    print(f"⏰ Получен ввод времени от {user_id}: {message.text}")
    
    time_text = message.text.strip()
    
    if f"notify_time_{user_id}" in user_states:
        del user_states[f"notify_time_{user_id}"]
    
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
    
    # Обновляем настройки
    update_user_settings(user_id, {
        'notifications': 1,
        'notify_time': time_string
    })
    
    show_notify_settings(chat_id, user_id)

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
        show_notify_settings(chat_id, user_id)
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
        update_user_settings(user_id, {
            'notifications': 1,
            'notify_time': '10:00,15:00,20:00',
            'timezone': 'UTC'
        })
        
        bot.answer_callback_query(call.id, "✅ Уведомления включены!")
        show_notify_settings(chat_id, user_id, message_id)
        return
    
    if call.data == "notify_off":
        update_user_settings(user_id, {'notifications': 0})
        
        bot.answer_callback_query(call.id, "✅ Уведомления выключены")
        show_notify_settings(chat_id, user_id, message_id)
        return
    
    if call.data == "notify_set_time":
        user_states[f"notify_time_{user_id}"] = {"step": "waiting"}
        
        settings = get_user_settings(user_id)
        current_times = settings.get('notify_time', "10:00,15:00,20:00") if settings else "10:00,15:00,20:00"
        
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
    
    if call.data == "notify_timezone":
        markup = telebot.types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            telebot.types.InlineKeyboardButton("🇷🇺 Москва (MSK, UTC+3)", callback_data="tz_Europe/Moscow"),
            telebot.types.InlineKeyboardButton("🇬🇧 Лондон (UTC)", callback_data="tz_Europe/London"),
            telebot.types.InlineKeyboardButton("🇪🇺 Берлин (CET, UTC+1)", callback_data="tz_Europe/Berlin"),
            telebot.types.InlineKeyboardButton("🇺🇸 Нью-Йорк (EST, UTC-5)", callback_data="tz_America/New_York"),
            telebot.types.InlineKeyboardButton("🔙 Назад", callback_data="notify_back")
        )
        
        try:
            bot.edit_message_text(
                "🌍 *Настройка часового пояса*\n\n"
                "Выбери свой часовой пояс, чтобы уведомления приходили в правильное время.\n"
                "Сейчас у тебя UTC (Лондон), поэтому уведомления приходят на 3 часа позже.",
                chat_id,
                message_id,
                parse_mode='Markdown',
                reply_markup=markup
            )
        except:
            bot.send_message(
                chat_id,
                "🌍 *Настройка часового пояса*\n\n"
                "Выбери свой часовой пояс, чтобы уведомления приходили в правильное время.\n"
                "Сейчас у тебя UTC (Лондон), поэтому уведомления приходят на 3 часа позже.",
                parse_mode='Markdown',
                reply_markup=markup
            )
        bot.answer_callback_query(call.id)
        return
    
    if call.data.startswith("tz_"):
        tz_name = call.data[3:]
        
        update_user_settings(user_id, {'timezone': tz_name})
        
        bot.answer_callback_query(call.id, f"✅ Часовой пояс установлен: {tz_name}")
        
        show_notify_settings(chat_id, user_id, message_id)
        return
    
    if call.data == "notify_back":
        if f"notify_time_{user_id}" in user_states:
            del user_states[f"notify_time_{user_id}"]
        
        show_notify_settings(chat_id, user_id, message_id)
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
        
        word_response = supabase.table('words')\
            .select('word')\
            .eq('id', word_id)\
            .execute()
        
        if word_response.data:
            word_text = word_response.data[0]['word']
            audio_bytes = generate_voice(word_text)
            if audio_bytes:
                bot.send_voice(chat_id, audio_bytes, caption=f"Произношение: {word_text}")
            else:
                bot.send_message(chat_id, "😕 Не удалось сгенерировать произношение.")
        else:
            bot.send_message(chat_id, "😕 Слово не найдено.")
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

        word_response = supabase.table('words').select('*').eq('id', word_id).execute()
        
        if word_response.data:
            word_data = word_response.data[0]
            word = {
                'id': word_data['id'],
                'word': word_data['word'],
                'translation': word_data['translation'],
                'example': word_data['example'],
                'example_translation': word_data['example_translation'],
                'synonyms': word_data['synonyms'],
                'part_of_speech': word_data['part_of_speech']
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
        
        try:
            # Получаем случайные варианты ответов
            response = supabase.table('words')\
                .select('translation')\
                .neq('id', word['id'])\
                .limit(3)\
                .execute()
            
            wrong_options = [row['translation'] for row in response.data]
            
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
            
        except Exception as e:
            print(f"Ошибка в continue_practice: {e}")
        
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

        word_response = supabase.table('words').select('*').eq('id', word_id).execute()

        if word_response.data:
            word_data = word_response.data[0]
            word = {
                'id': word_data['id'],
                'word': word_data['word'],
                'translation': word_data['translation'],
                'example': word_data['example'],
                'example_translation': word_data['example_translation'],
                'synonyms': word_data['synonyms'],
                'part_of_speech': word_data['part_of_speech']
            }

            # Проверяем, сохранено ли слово
            existing = supabase.table('user_words')\
                .select('*')\
                .eq('user_id', user_id)\
                .eq('word_id', word_id)\
                .execute()
            
            is_saved = len(existing.data) > 0
            
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

        word_response = supabase.table('words').select('*').eq('id', word_id).execute()

        if word_response.data:
            word_data = word_response.data[0]
            word = {
                'id': word_data['id'],
                'word': word_data['word'],
                'translation': word_data['translation'],
                'example': word_data['example'],
                'example_translation': word_data['example_translation'],
                'synonyms': word_data['synonyms'],
                'part_of_speech': word_data['part_of_speech']
            }

            bot.answer_callback_query(call.id, "👀 Вот правильный ответ!")

            # Проверяем, сохранено ли слово
            existing = supabase.table('user_words')\
                .select('*')\
                .eq('user_id', user_id)\
                .eq('word_id', word_id)\
                .execute()
            
            is_saved = len(existing.data) > 0
            
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

# ----- ОБРАБОТЧИК ТЕКСТА (ПОИСК СЛОВ С AI) -----
@bot.message_handler(func=lambda m: True)
def handle_text(message):
    if message.text.startswith('/'):
        return
        
    word_text = message.text.strip().lower()

    # Сначала ищем в базе
    word_response = supabase.table('words')\
        .select('*')\
        .ilike('word', word_text)\
        .execute()

    user_id = get_user_id(message)
    if not user_id:
        bot.send_message(message.chat.id, "😕 Не удалось определить пользователя")
        return
    
    # Если слово есть в базе
    if word_response.data:
        word_data = word_response.data[0]
        word = {
            'id': word_data['id'],
            'word': word_data['word'],
            'translation': word_data['translation'],
            'example': word_data['example'],
            'example_translation': word_data['example_translation'],
            'synonyms': word_data['synonyms'],
            'part_of_speech': word_data['part_of_speech']
        }
        
        # Проверяем, сохранено ли слово
        existing = supabase.table('user_words')\
            .select('*')\
            .eq('user_id', user_id)\
            .eq('word_id', word['id'])\
            .execute()
        
        is_saved = len(existing.data) > 0
        
        card = format_word_card(word, user_id=user_id)
        markup = get_unified_keyboard(
            word_id=word['id'],
            mode="search",
            is_saved=is_saved
        )
        bot.send_message(message.chat.id, card, parse_mode='Markdown', reply_markup=markup)
        return
    
    # Если слова нет в базе — пробуем нейросеть
    status_msg = bot.send_message(message.chat.id, "🤔 Слова нет в словаре. Ищу через ИИ...")
    
    ai_word = get_word_from_ai(word_text)
    
    if ai_word:
        # Автоматически сохраняем в общую базу
        word_response = supabase.table('words').insert({
            'word': ai_word['word'],
            'translation': ai_word['translation'],
            'example': ai_word['example'],
            'example_translation': ai_word['example_translation'],
            'synonyms': ai_word['synonyms'],
            'part_of_speech': ai_word['part_of_speech']
        }).execute()
        
        # Получаем ID слова
        if word_response.data:
            word_id = word_response.data[0]['id']
        else:
            # Если не удалось получить ID, ищем слово
            find_response = supabase.table('words')\
                .select('id')\
                .eq('word', ai_word['word'])\
                .execute()
            word_id = find_response.data[0]['id'] if find_response.data else None
        
        card = format_word_card(ai_word, user_id=user_id)
        
        markup = get_unified_keyboard(
            word_id=word_id,
            mode="search",
            is_saved=False
        )
        
        bot.edit_message_text(
            card, 
            message.chat.id, 
            status_msg.message_id,
            parse_mode='Markdown', 
            reply_markup=markup
        )
    else:
        bot.edit_message_text(
            f"😕 Не знаю слова '{message.text}'. Попробуй другое или зайди в меню.",
            message.chat.id,
            status_msg.message_id
        )

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
