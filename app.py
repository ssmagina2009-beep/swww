import logging
import json
import re
import os
from datetime import datetime, timedelta

from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
import requests

# ===================== SETTINGS =====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# Reminder times (hours before event)
REMINDER_DELTAS = [168, 48, 24, 0]

# ===================== LOGGING =====================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ===================== INIT =====================
app = Flask(__name__)

# ===================== STORAGE =====================
events = []
scheduled_jobs = {}

# ===================== SCHEDULER =====================
scheduler = BackgroundScheduler()
scheduler.start()

# ===================== DATE/TIME PARSER =====================
def parse_date_time(date_str, time_str):
    """Собирает datetime из date и time от Dialogflow."""
    if not date_str:
        return None
    date_str = str(date_str).strip()
    time_str = str(time_str).strip() if time_str else ""
    try:
        if time_str:
            dt_str = f"{date_str} {time_str}"
            for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%d.%m.%y %H:%M"]:
                try:
                    return datetime.strptime(dt_str, fmt)
                except ValueError:
                    continue
        else:
            for fmt in ["%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"]:
                try:
                    return datetime.strptime(date_str, fmt)
                except ValueError:
                    continue
    except Exception:
        pass
    return None

def parse_from_text(text):
    """Парсит дату-время из произвольного текста."""
    if not text:
        return None
    try:
        return parse_date_time(text, None)
    except Exception:
        return None

def format_dt(dt):
    return dt.strftime("%d.%m.%Y %H:%M")

# ===================== TELEGRAM API =====================
def send_telegram_message(chat_id, text, reply_markup=None):
    """Отправка сообщения через Telegram Bot API."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")

def get_main_keyboard():
    """Главная клавиатура с кнопками."""
    return {
        "keyboard": [
            [{"text": "👋 Привет"}],
            [{"text": "📅 Создать событие"}],
            [{"text": "🏆 Новая олимпиада"}],
            [{"text": "🗑 Удалить событие"}]
        ],
        "resize_keyboard": True
    }

# ===================== SCHEDULER =====================
def schedule_reminders(chat_id, event_type, name, start_dt, end_dt, subject=None, level=None):
    job_ids = []
    now = datetime.now()

    for delta_hours in REMINDER_DELTAS:
        reminder_time = start_dt - timedelta(hours=delta_hours)
        if reminder_time <= now:
            continue

        job_id = f"{chat_id}_{name}_{start_dt.isoformat()}_{delta_hours}"

        hours_text = str(delta_hours) if delta_hours > 0 else "0"
        start_str = format_dt(start_dt)
        end_str = format_dt(end_dt)

        if event_type == "olympiad":
            message = (f"Напоминание! Через «{hours_text}» часов будет олимпиада "
                       f"«{name}» {subject} {level} уровня "
                       f"с {start_str} по {end_str}")
        else:
            message = (f"Напоминание! Через «{hours_text}» часов будет событие "
                       f"«{name}» с {start_str} по {end_str}")

        # Добавляем задачу в APScheduler
        scheduler.add_job(
            send_telegram_message,
            trigger=DateTrigger(run_date=reminder_time),
            args=[chat_id, message],
            id=job_id,
            replace_existing=True
        )

        scheduled_jobs[job_id] = {
            "chat_id": chat_id,
            "data": {
                "event_type": event_type,
                "name": name,
                "start_dt": start_dt,
                "end_dt": end_dt,
                "subject": subject,
                "level": level,
                "delta_hours": delta_hours,
            }
        }
        job_ids.append(job_id)

    return job_ids

def remove_reminder_jobs(job_ids):
    """Удаление запланированных задач."""
    for job_id in job_ids:
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass
        if job_id in scheduled_jobs:
            del scheduled_jobs[job_id]

# ===================== PARAM EXTRACTOR =====================
def get_param(params, name, default=None):
    val = params.get(name, default)
    if isinstance(val, dict):
        return val.get("name") or val.get("value") or str(val)
    return val

def extract_datetime(params, date_key, time_key):
    """Извлекает datetime из параметров Dialogflow."""
    d = get_param(params, date_key)
    t = get_param(params, time_key)
    if d:
        return parse_date_time(d, t)
    return None

# ===================== WEBHOOK =====================
@app.route('/webhook', methods=['POST'])
def webhook():
    req = request.get_json(silent=True, force=True)
    logger.info(f"Webhook received: {json.dumps(req, ensure_ascii=False)}")

    intent_name = req.get("queryResult", {}).get("intent", {}).get("displayName", "").lower()
    params = req.get("queryResult", {}).get("parameters", {})
    query_text = req.get("queryResult", {}).get("queryText", "")

    # Получаем chat_id из Telegram контекста
    output_contexts = req.get("queryResult", {}).get("outputContexts", [])
    chat_id = None

    for ctx in output_contexts:
        ctx_name = ctx.get("name", "")
        ctx_params = ctx.get("parameters", {})
        if "telegram" in ctx_name.lower():
            chat_id = ctx_params.get("chat_id") or ctx_params.get("chatId") or ctx_params.get("from_id")
        if not chat_id:
            chat_id = ctx_params.get("chat_id") or ctx_params.get("chatId")

    # Fallback: originalDetectIntentRequest
    if not chat_id:
        orig = req.get("originalDetectIntentRequest", {})
        payload = orig.get("payload", {})
        telegram_data = payload.get("data", {})
        if "message" in telegram_data:
            chat_id = telegram_data["message"]["chat"]["id"]
        elif "callback_query" in telegram_data:
            chat_id = telegram_data["callback_query"]["message"]["chat"]["id"]

    # Fallback: session
    if not chat_id:
        session = req.get("session", "")
        if "/sessions/" in session:
            chat_id = session.split("/sessions/")[-1]

    if not chat_id:
        logger.error("chat_id not found!")
        return jsonify({"fulfillmentText": "Ошибка: не удалось определить чат."})

    try:
        chat_id = int(chat_id)
    except:
        pass

    logger.info(f"Intent: {intent_name}, chat_id: {chat_id}, params: {params}")

    # --- REMINDER / EVENT ---
    if "napomin" in intent_name or "reminder" in intent_name or "sobytie" in intent_name or "event" in intent_name:
        return handle_reminder(params, query_text, chat_id)

    # --- OLYMPIAD ---
    elif "olimpiad" in intent_name or "olympiad" in intent_name:
        return handle_olympiad(params, query_text, chat_id)

    # --- CANCEL / DELETE ---
    elif "otmen" in intent_name or "cancel" in intent_name or "delete" in intent_name or "udal" in intent_name:
        return handle_cancel(params, query_text, chat_id)

    # --- GREETING / START ---
    elif "privet" in intent_name or "start" in intent_name or "hello" in intent_name or "greeting" in intent_name:
        return handle_greeting(chat_id)

    return jsonify({"fulfillmentText": "Не понял команду. Используйте кнопки меню."})

# ===================== HANDLERS =====================

def handle_greeting(chat_id):
    """Приветствие и показ кнопок."""
    welcome_text = (
        "👋 Привет! Я бот для напоминаний!\n\n"
        "📌 <b>Что я умею:</b>\n"
        "• Создавать события с напоминаниями\n"
        "• Добавлять олимпиады с отслеживанием\n"
        "• Удалять созданные события\n\n"
        "📝 <b>Как создать событие:</b>\n"
        "Напишите: «создать событие Название дд.мм.гггг чч:мм»\n\n"
        "🏆 <b>Как добавить олимпиаду:</b>\n"
        "Напишите: «олимпиада Название дд.мм.гггг дд.мм.гггг Предмет Уровень»\n\n"
        "🗑 <b>Как удалить:</b>\n"
        "Напишите: «отменить Название дд.мм.гггг»\n\n"
        "Используйте кнопки ниже для удобства!"
    )
    send_telegram_message(chat_id, welcome_text, get_main_keyboard())
    return jsonify({"fulfillmentText": "Привет! Я показал меню с кнопками."})

def handle_reminder(params, query_text, chat_id):
    """
    Параметры Dialogflow:
    - name: @sys.any (название события)
    - date: @sys.date (дата начала)
    - time: @sys.time (время начала)
    """
    name = get_param(params, "name")
    start_dt = extract_datetime(params, "date", "time")

    # Парсим дату окончания из текста запроса
    end_dt = None
    end_match = re.search(r"(?:по|до)\s+([0-9]{1,2}[./-][0-9]{1,2}[./-][0-9]{2,4}(?:\s+[0-9]{1,2}:[0-9]{2})?)", query_text, re.IGNORECASE)
    if end_match:
        end_dt = parse_from_text(end_match.group(1))

    if not end_dt:
        all_dates = re.findall(r"[0-9]{1,2}[./-][0-9]{1,2}[./-][0-9]{2,4}(?:\s+[0-9]{1,2}:[0-9]{2})?", query_text)
        if len(all_dates) >= 2:
            end_dt = parse_from_text(all_dates[1])

    # Fallback: end = start + 1 час
    if not end_dt and start_dt:
        end_dt = start_dt + timedelta(hours=1)

    if not name or not start_dt:
        return jsonify({"fulfillmentText": "Укажите название, дату и время начала события."})

    job_ids = schedule_reminders(chat_id, "event", name, start_dt, end_dt)

    events.append({
        "chat_id": chat_id,
        "event_type": "event",
        "name": name,
        "start_dt": start_dt,
        "end_dt": end_dt,
        "subject": None,
        "level": None,
        "job_ids": job_ids,
    })

    response_text = f"Добавлено новое событие «{name}» с {format_dt(start_dt)} по {format_dt(end_dt)}"
    logger.info(f"Reminder response: {response_text}")

    return jsonify({"fulfillmentText": response_text})

def handle_olympiad(params, query_text, chat_id):
    """
    Параметры Dialogflow:
    - any: @sys.any (название олимпиады)
    - subject: @subject (предмет)
    - level: @level (уровень 1/2/3)
    - FROM: @sys.date (дата начала)
    - to: @sys.date (дата окончания)
    """
    name = get_param(params, "any")
    subject = get_param(params, "subject")
    level = get_param(params, "level")

    start_date = get_param(params, "FROM")
    end_date = get_param(params, "to")

    start_dt = parse_date_time(start_date, None) if start_date else None
    end_dt = parse_date_time(end_date, None) if end_date else None

    # Fallback: парсим из текста
    if not start_dt or not end_dt:
        all_dates = re.findall(r"[0-9]{1,2}[./-][0-9]{1,2}[./-][0-9]{2,4}", query_text)
        if len(all_dates) >= 2:
            if not start_dt:
                start_dt = parse_from_text(all_dates[0])
            if not end_dt:
                end_dt = parse_from_text(all_dates[1])

    # Добавляем время по умолчанию (олимпиады обычно с 9:00 до 14:00)
    if start_dt and start_dt.hour == 0 and start_dt.minute == 0:
        start_dt = start_dt.replace(hour=9, minute=0)
    if end_dt and end_dt.hour == 0 and end_dt.minute == 0:
        end_dt = end_dt.replace(hour=14, minute=0)

    if not name or not start_dt or not end_dt:
        return jsonify({"fulfillmentText": "Укажите название олимпиады, предмет, уровень и даты."})

    job_ids = schedule_reminders(chat_id, "olympiad", name, start_dt, end_dt, subject, level)

    events.append({
        "chat_id": chat_id,
        "event_type": "olympiad",
        "name": name,
        "start_dt": start_dt,
        "end_dt": end_dt,
        "subject": subject,
        "level": level,
        "job_ids": job_ids,
    })

    response_text = f"Добавлена новая олимпиада «{name}» {subject} {level} уровня с {format_dt(start_dt)} по {format_dt(end_dt)}"
    logger.info(f"Olympiad response: {response_text}")

    return jsonify({"fulfillmentText": response_text})

def handle_cancel(params, query_text, chat_id):
    """
    Параметры Dialogflow:
    - any: @sys.any (название события/олимпиады)
    - date: @sys.date (дата начала)
    """
    name = get_param(params, "any")
    date_val = get_param(params, "date")

    start_dt = parse_date_time(date_val, None) if date_val else None

    # Fallback: парсим дату-время из текста
    if not start_dt:
        all_dates = re.findall(r"[0-9]{1,2}[./-][0-9]{1,2}[./-][0-9]{2,4}(?:\s+[0-9]{1,2}:[0-9]{2})?", query_text)
        if all_dates:
            start_dt = parse_from_text(all_dates[0])

    if not name:
        return jsonify({"fulfillmentText": "Укажите название события или олимпиады для удаления."})

    found = False
    for i, event in enumerate(events):
        if event["chat_id"] != chat_id:
            continue

        # Проверяем совпадение по названию
        title_match = name.lower() in event["name"].lower() or event["name"].lower() in name.lower()

        # Проверяем совпадение по дате (если указана)
        date_match = True
        if start_dt:
            event_date = event["start_dt"].strftime("%d.%m.%Y")
            search_date = start_dt.strftime("%d.%m.%Y")
            date_match = event_date == search_date

        if title_match and date_match:
            remove_reminder_jobs(event["job_ids"])
            event_type = "событие" if event["event_type"] == "event" else "олимпиада"
            del events[i]
            return jsonify({"fulfillmentText": f"Удалил {event_type} «{event['name']}»"})

    return jsonify({"fulfillmentText": "Не нашел событие"})

# ===================== HEALTH CHECK =====================
@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "ok",
        "events_count": len(events),
        "scheduled_jobs": len(scheduled_jobs)
    })

@app.route('/')
def index():
    return "Reminder Bot is running!"

# ===================== RUN =====================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
