import os
import json
import re
import pytz
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
import requests

app = Flask(__name__)

# ─── Конфигурация ─────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Хранилище напоминаний (в продакшене лучше использовать БД)
# Структура: {chat_id: [{"type": "event"/"olympiad", "title": ..., "start": ..., ...}]}
reminders_db = {}

scheduler = BackgroundScheduler(timezone=pytz.timezone('Europe/Moscow'))
scheduler.start()

# ─── Вспомогательные функции ──────────────────────────────────────────────────

def send_message(chat_id, text, reply_markup=None):
    """Отправка сообщения в Telegram"""
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)

    try:
        requests.post(f"{TELEGRAM_API}/sendMessage", json=payload)
    except Exception as e:
        print(f"Ошибка отправки: {e}")

def get_main_keyboard():
    """Главная клавиатура с кнопками"""
    return {
        "keyboard": [
            [{"text": "👋 Привет"}],
            [{"text": "📅 Создать событие"}],
            [{"text": "🏆 Новая олимпиада"}],
            [{"text": "🗑 Удалить событие"}]
        ],
        "resize_keyboard": True
    }

def parse_datetime(date_str, time_str=None):
    """Парсинг даты и времени из строки"""
    formats = [
        "%d.%m.%Y %H:%M",
        "%d.%m.%y %H:%M",
        "%Y-%m-%d %H:%M",
        "%d/%m/%Y %H:%M",
        "%d.%m.%Y",
        "%d.%m.%y",
        "%Y-%m-%d",
        "%d/%m/%Y"
    ]

    combined = f"{date_str} {time_str}" if time_str else date_str

    for fmt in formats:
        try:
            dt = datetime.strptime(combined.strip(), fmt)
            # Если время не указано, ставим 00:00
            if time_str is None and "%H:%M" not in fmt:
                dt = dt.replace(hour=0, minute=0)
            return dt
        except ValueError:
            continue
    return None

def schedule_reminder(chat_id, reminder_type, title, start_dt, extra_info=None):
    """Планирование напоминаний"""

    # Время напоминаний: 168ч, 48ч, 24ч и в момент события
    reminder_times = [
        (168, "168"),
        (48, "48"),
        (24, "24"),
        (0, "0")
    ]

    job_ids = []

    for hours_before, hours_label in reminder_times:
        reminder_dt = start_dt - timedelta(hours=hours_before)

        # Не планируем, если время уже прошло
        if reminder_dt < datetime.now(pytz.timezone('Europe/Moscow')).replace(tzinfo=None):
            continue

        job_id = f"{chat_id}_{reminder_type}_{title}_{start_dt.strftime('%Y%m%d%H%M')}_{hours_before}"

        if reminder_type == "event":
            message = f'напоминание! Через «{hours_label}» часов будет событие «{title}» с {start_dt.strftime("%d.%m.%Y %H:%M")}'
        else:  # olympiad
            subject = extra_info.get("subject", "")
            level = extra_info.get("level", "")
            end_dt = extra_info.get("end_dt", start_dt)
            message = f'напоминание! Через «{hours_label}» часов будет олимпиада «{title}» {subject} {level} уровня с {start_dt.strftime("%d.%m.%Y")} по {end_dt.strftime("%d.%m.%Y")}'

        scheduler.add_job(
            send_message,
            trigger=DateTrigger(run_date=reminder_dt),
            args=[chat_id, message],
            id=job_id,
            replace_existing=True
        )
        job_ids.append(job_id)

    return job_ids

def remove_reminder_jobs(job_ids):
    """Удаление запланированных задач"""
    for job_id in job_ids:
        try:
            scheduler.remove_job(job_id)
        except:
            pass

# ─── Обработчики команд и сообщений ───────────────────────────────────────────

def handle_start(chat_id):
    """Обработка команды /start и кнопки Привет"""
    welcome_text = (
        "👋 Привет! Я бот для напоминаний!

"
        "📌 <b>Что я умею:</b>
"
        "• Создавать события с напоминаниями
"
        "• Добавлять олимпиады с отслеживанием
"
        "• Удалять созданные события

"
        "📝 <b>Как создать событие:</b>
"
        "Напишите: «создать событие Название дд.мм.гггг чч:мм»

"
        "🏆 <b>Как добавить олимпиаду:</b>
"
        "Напишите: «олимпиада Название дд.мм.гггг дд.мм.гггг Предмет Уровень»

"
        "🗑 <b>Как удалить:</b>
"
        "Напишите: «отменить Название дд.мм.гггг»

"
        "Используйте кнопки ниже для удобства!"
    )
    send_message(chat_id, welcome_text, get_main_keyboard())

def handle_create_event(chat_id, text):
    """Создание события/напоминания"""
    # Паттерны для поиска данных
    # Форматы: "создать/сделать/создай/сделай событие/напоминание Название дата время"

    # Убираем ключевые слова из начала
    cleaned = re.sub(r'^(создать|сделать|создай|сделай|напоминание|событие)\s+', '', text, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+(создать|сделать|создай|сделай|напоминание|событие)\s+', ' ', cleaned, flags=re.IGNORECASE)

    # Ищем дату и время
    # Паттерны дат: дд.мм.гггг, дд.мм.гг, гггг-мм-дд
    date_patterns = [
        r'(\d{1,2}\.\d{1,2}\.\d{4})\s+(\d{1,2}:\d{2})',  # дд.мм.гггг чч:мм
        r'(\d{1,2}\.\d{1,2}\.\d{2})\s+(\d{1,2}:\d{2})',   # дд.мм.гг чч:мм
        r'(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})',          # гггг-мм-дд чч:мм
        r'(\d{1,2}\/\d{1,2}\/\d{4})\s+(\d{1,2}:\d{2})',   # дд/мм/гггг чч:мм
        r'(\d{1,2}\.\d{1,2}\.\d{4})',                      # только дата
        r'(\d{1,2}\.\d{1,2}\.\d{2})',
        r'(\d{4}-\d{2}-\d{2})',
    ]

    start_dt = None
    title = cleaned

    for pattern in date_patterns:
        match = re.search(pattern, cleaned)
        if match:
            if len(match.groups()) == 2:
                start_dt = parse_datetime(match.group(1), match.group(2))
            else:
                start_dt = parse_datetime(match.group(1))

            if start_dt:
                # Убираем дату и время из названия
                title = cleaned[:match.start()].strip()
                break

    if not start_dt:
        send_message(chat_id, "❌ Не удалось распознать дату и время.
Формат: «создать событие Название дд.мм.гггг чч:мм»")
        return

    if not title:
        send_message(chat_id, "❌ Укажите название события.")
        return

    # Сохраняем в базу
    if chat_id not in reminders_db:
        reminders_db[chat_id] = []

    reminder = {
        "type": "event",
        "title": title,
        "start": start_dt,
        "job_ids": []
    }

    # Планируем напоминания
    job_ids = schedule_reminder(chat_id, "event", title, start_dt)
    reminder["job_ids"] = job_ids
    reminders_db[chat_id].append(reminder)

    send_message(
        chat_id,
        f'✅ добавлено новое событие «{title}» с {start_dt.strftime("%d.%m.%Y %H:%M")}'
    )

def handle_create_olympiad(chat_id, text):
    """Создание олимпиады"""
    # Формат: "олимпиада Название дата_начала дата_конца Предмет Уровень"
    # Убираем ключевое слово
    cleaned = re.sub(r'^олимпиада\s+', '', text, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+олимпиада\s+', ' ', cleaned, flags=re.IGNORECASE)

    # Ищем две даты
    date_pattern = r'(\d{1,2}\.\d{1,2}\.\d{4})'
    dates = re.findall(date_pattern, cleaned)

    if len(dates) < 1:
        send_message(chat_id, "❌ Не удалось распознать даты.
Формат: «олимпиада Название дд.мм.гггг дд.мм.гггг Предмет Уровень»")
        return

    start_dt = parse_datetime(dates[0])
    end_dt = parse_datetime(dates[1]) if len(dates) > 1 else start_dt

    if not start_dt:
        send_message(chat_id, "❌ Неверный формат даты начала.")
        return

    # Убираем даты из текста
    temp_text = re.sub(date_pattern, '', cleaned, count=2)
    parts = [p.strip() for p in temp_text.split() if p.strip()]

    # Последнее слово — уровень (1/2/3)
    level = None
    if parts and parts[-1] in ['1', '2', '3']:
        level = parts[-1]
        parts = parts[:-1]

    # Предмет — предпоследнее (или последнее, если уровень не найден)
    subject = ""
    if parts:
        subject = parts[-1]
        parts = parts[:-1]

    # Оставшееся — название
    title = ' '.join(parts) if parts else "Олимпиада"

    if not level:
        send_message(chat_id, "❌ Укажите уровень олимпиады (1, 2 или 3).")
        return

    # Сохраняем
    if chat_id not in reminders_db:
        reminders_db[chat_id] = []

    reminder = {
        "type": "olympiad",
        "title": title,
        "start": start_dt,
        "end": end_dt,
        "subject": subject,
        "level": level,
        "job_ids": []
    }

    extra_info = {"subject": subject, "level": level, "end_dt": end_dt}
    job_ids = schedule_reminder(chat_id, "olympiad", title, start_dt, extra_info)
    reminder["job_ids"] = job_ids
    reminders_db[chat_id].append(reminder)

    send_message(
        chat_id,
        f'✅ добавлена новая олимпиада «{title}» {subject} {level} уровня с {start_dt.strftime("%d.%m.%Y")} по {end_dt.strftime("%d.%m.%Y")}'
    )

def handle_delete_event(chat_id, text):
    """Удаление события или олимпиады"""
    # Убираем ключевые слова
    cleaned = re.sub(r'^(отмени|отмена|отменить|удали|удалить|удаление)\s+', '', text, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+(отмени|отмена|отменить|удали|удалить|удаление)\s+', ' ', cleaned, flags=re.IGNORECASE)

    # Ищем дату
    date_pattern = r'(\d{1,2}\.\d{1,2}\.\d{4})'
    match = re.search(date_pattern, cleaned)

    search_date = None
    title = cleaned

    if match:
        search_date = parse_datetime(match.group(1))
        title = cleaned[:match.start()].strip()

    if chat_id not in reminders_db or not reminders_db[chat_id]:
        send_message(chat_id, "❌ не нашел событие")
        return

    found = False
    for i, reminder in enumerate(reminders_db[chat_id]):
        # Проверяем совпадение по названию
        title_match = title.lower() in reminder["title"].lower() or reminder["title"].lower() in title.lower()

        # Проверяем совпадение по дате (если указана)
        date_match = True
        if search_date:
            reminder_date = reminder["start"].strftime("%d.%m.%Y")
            search_date_str = search_date.strftime("%d.%m.%Y")
            date_match = reminder_date == search_date_str

        if title_match and date_match:
            # Удаляем задачи
            remove_reminder_jobs(reminder["job_ids"])
            # Удаляем из базы
            event_type = "событие" if reminder["type"] == "event" else "олимпиада"
            del reminders_db[chat_id][i]
            send_message(chat_id, f'🗑 удалил {event_type} «{reminder["title"]}»')
            found = True
            break

    if not found:
        send_message(chat_id, "❌ не нашел событие")

def process_message(chat_id, text):
    """Главный обработчик входящих сообщений"""
    text_lower = text.lower()

    # Кнопки меню
    if text == "👋 Привет" or text.lower() in ['/start', 'привет', 'hello', 'hi']:
        handle_start(chat_id)
        return

    if text == "📅 Создать событие":
        send_message(
            chat_id,
            "📝 Для создания события напишите:
"
            "«создать событие Название дд.мм.гггг чч:мм»

"
            "Пример: создать событие Экзамен по математике 25.12.2024 09:00",
            get_main_keyboard()
        )
        return

    if text == "🏆 Новая олимпиада":
        send_message(
            chat_id,
            "🏆 Для добавления олимпиады напишите:
"
            "«олимпиада Название дд.мм.гггг дд.мм.гггг Предмет Уровень»

"
            "Пример: олимпиада Всероссийская олимпиада 15.01.2025 20.01.2025 Математика 3",
            get_main_keyboard()
        )
        return

    if text == "🗑 Удалить событие":
        send_message(
            chat_id,
            "🗑 Для удаления напишите:
"
            "«отменить Название дд.мм.гггг»

"
            "Пример: отменить Экзамен по математике 25.12.2024",
            get_main_keyboard()
        )
        return

    # Определяем тип сообщения по ключевым словам
    event_keywords = ['напоминание', 'событие', 'сделать', 'сделай', 'создай', 'создать']
    olympiad_keywords = ['олимпиада']
    delete_keywords = ['отмени', 'отмена', 'отменить', 'удали', 'удалить']

    is_event = any(kw in text_lower for kw in event_keywords)
    is_olympiad = any(kw in text_lower for kw in olympiad_keywords)
    is_delete = any(kw in text_lower for kw in delete_keywords)

    if is_delete:
        handle_delete_event(chat_id, text)
    elif is_olympiad:
        handle_create_olympiad(chat_id, text)
    elif is_event:
        handle_create_event(chat_id, text)
    else:
        # Не распознали — показываем подсказку
        send_message(
            chat_id,
            "🤔 Я не понял команду.

"
            "Используйте кнопки меню или напишите:
"
            "• «создать событие Название дата время»
"
            "• «олимпиада Название дата_начала дата_конца Предмет Уровень»
"
            "• «отменить Название дата»",
            get_main_keyboard()
        )

# ─── Webhook endpoints ────────────────────────────────────────────────────────

@app.route('/webhook', methods=['POST'])
def webhook():
    """Основной webhook для Dialogflow / Telegram"""
    data = request.get_json(silent=True, force=True)

    # Dialogflow формат
    if 'originalDetectIntentRequest' in data:
        payload = data.get('originalDetectIntentRequest', {}).get('payload', {})
        if 'data' in payload:
            # Telegram update через Dialogflow
            telegram_data = payload['data']
            if 'message' in telegram_data:
                chat_id = telegram_data['message']['chat']['id']
                text = telegram_data['message'].get('text', '')
                process_message(chat_id, text)

        # Ответ для Dialogflow
        return jsonify({
            "fulfillmentText": "Обработано",
            "fulfillmentMessages": [{"text": {"text": ["Обработано"]}}]
        })

    # Прямой Telegram webhook
    if 'message' in data:
        chat_id = data['message']['chat']['id']
        text = data['message'].get('text', '')
        process_message(chat_id, text)
        return jsonify({"status": "ok"})

    return jsonify({"status": "unknown_format"})

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({"status": "ok", "reminders_count": sum(len(v) for v in reminders_db.values())})

@app.route('/')
def index():
    return "Reminder Bot is running!"

# ─── Запуск ───────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
