import logging
import json
import re
import os
from datetime import datetime, timedelta

from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
import requests
import pytz

# ===================== SETTINGS =====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "NOT_SET")

# Часовой пояс пользователя (UTC+5 — определяется по Dialogflow или дефолт)
USER_TIMEZONE = pytz.timezone('Asia/Yekaterinburg')  # UTC+5
UTC = pytz.UTC

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
scheduler = BackgroundScheduler(timezone=UTC)
scheduler.start()

# ===================== DATE/TIME PARSER =====================
def parse_date_time(date_str, time_str):
    """Собирает datetime из date и time от Dialogflow с учётом часового пояса.

    Извлекает дату из date_str и время из time_str отдельно, чтобы избежать
    конфликтов, когда Dialogflow добавляет дефолтное время к дате.
    """
    if not date_str:
        return None
    date_str = str(date_str).strip()
    time_str = str(time_str).strip() if time_str else ""

    try:
        # Извлекаем дату (YYYY-MM-DD) из date_str
        if 'T' in date_str:
            date_part = date_str.split('T')[0]
        else:
            date_part = date_str

        # Извлекаем время (HH:MM:SS) из time_str
        time_part = None
        if time_str:
            if 'T' in time_str:
                # Формат: 2026-07-17T03:34:00+05:00 → берём 03:34:00
                time_part = time_str.split('T')[1].split('+')[0].split('Z')[0]
            else:
                time_part = time_str

        if time_part:
            # Склеиваем дату и время
            dt_str = f"{date_part}T{time_part}"
            dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S")
            dt = USER_TIMEZONE.localize(dt)
            return dt
        else:
            # Только дата, без времени
            dt = datetime.strptime(date_part, "%Y-%m-%d")
            dt = USER_TIMEZONE.localize(dt)
            return dt

    except Exception as e:
        logger.error(f"Parse error: {e} for date={date_str}, time={time_str}")
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
    if BOT_TOKEN == "NOT_SET":
        logger.error("BOT_TOKEN not set!")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(url, json=payload, timeout=10)
        logger.info(f"Telegram API response: {r.status_code}")
    except Exception as e:
        logger.error(f"Send error: {e}")

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
    now = datetime.now(USER_TIMEZONE)  # Текущее время в часовом поясе пользователя

    logger.info(f"Scheduling reminders. Now: {now}, Event start: {start_dt}")

    for delta_hours in REMINDER_DELTAS:
        reminder_time = start_dt - timedelta(hours=delta_hours)

        # Конвертируем в UTC для APScheduler
        reminder_time_utc = reminder_time.astimezone(UTC)
        now_utc = now.astimezone(UTC)

        if reminder_time_utc <= now_utc:
            logger.info(f"Skipping reminder {delta_hours}h: time passed ({reminder_time_utc} <= {now_utc})")
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

        scheduler.add_job(
            send_telegram_message,
            trigger=DateTrigger(run_date=reminder_time_utc),
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
        logger.info(f"Scheduled reminder: {job_id} at {reminder_time_utc} UTC (local: {reminder_time})")

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
    """Извлекает параметр из Dialogflow, обрабатывает объекты."""
    val = params.get(name, default)
    if isinstance(val, dict):
        if "date_time" in val:
            return val["date_time"]
        return val.get("name") or val.get("value") or str(val)
    return val

def extract_datetime(params, date_key, time_key):
    """Извлекает datetime из параметров Dialogflow с учётом часового пояса."""
    d = get_param(params, date_key)
    t = get_param(params, time_key)
    if d:
        return parse_date_time(d, t)
    return None

# ===================== PARSER FROM TEXT =====================
def parse_event_from_text(text):
    """Парсит название, дату и время из текста сообщения."""
    cleaned = re.sub(r'^(создать|сделать|создай|сделай|напоминание|событие|event|reminder)\s+', '', text, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+(создать|сделать|создай|сделай|напоминание|событие|event|reminder)\s+', ' ', cleaned, flags=re.IGNORECASE)

    date_patterns = [
        r'(\d{1,2}\.\d{1,2}\.\d{4})\s+(\d{1,2}:\d{2})',
        r'(\d{1,2}\.\d{1,2}\.\d{2})\s+(\d{1,2}:\d{2})',
        r'(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})',
        r'(\d{1,2}\/\d{1,2}\/\d{4})\s+(\d{1,2}:\d{2})',
        r'(\d{1,2}\.\d{1,2}\.\d{4})',
        r'(\d{1,2}\.\d{1,2}\.\d{2})',
        r'(\d{4}-\d{2}-\d{2})',
    ]

    start_dt = None
    title = cleaned.strip()

    for pattern in date_patterns:
        match = re.search(pattern, cleaned)
        if match:
            if len(match.groups()) == 2:
                start_dt = parse_date_time(match.group(1), match.group(2))
            else:
                start_dt = parse_date_time(match.group(1), None)

            if start_dt:
                title = cleaned[:match.start()].strip()
                break

    return title, start_dt

def parse_olympiad_from_text(text):
    """Парсит олимпиаду из текста: название, дата_начала, дата_конца, предмет, уровень."""
    cleaned = re.sub(r'^олимпиада\s+', '', text, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+олимпиада\s+', ' ', cleaned, flags=re.IGNORECASE)

    date_pattern = r'(\d{1,2}\.\d{1,2}\.\d{4})'
    dates = re.findall(date_pattern, cleaned)

    start_dt = parse_date_time(dates[0], None) if len(dates) > 0 else None
    end_dt = parse_date_time(dates[1], None) if len(dates) > 1 else None

    temp_text = re.sub(date_pattern, '', cleaned, count=2)
    parts = [p.strip() for p in temp_text.split() if p.strip()]

    level = None
    if parts and parts[-1] in ['1', '2', '3']:
        level = parts[-1]
        parts = parts[:-1]

    subject = ""
    if parts:
        subject = parts[-1]
        parts = parts[:-1]

    title = ' '.join(parts) if parts else "Олимпиада"

    return title, start_dt, end_dt, subject, level

# ===================== WEBHOOK =====================
@app.route('/render', methods=['POST'])
def render_webhook():
    """Обработчик для Dialogflow, если URL указан как /render"""
    return webhook()

@app.route('/webhook', methods=['POST'])
def webhook():
    req = request.get_json(silent=True, force=True)
    logger.info(f"=== WEBHOOK RECEIVED ===")
    logger.info(json.dumps(req, ensure_ascii=False, indent=2)[:1000])

    intent_name = req.get("queryResult", {}).get("intent", {}).get("displayName", "").lower()
    params = req.get("queryResult", {}).get("parameters", {})
    query_text = req.get("queryResult", {}).get("queryText", "")

    # Проверяем, все ли параметры собраны (для slot filling)
    all_required_present = req.get("queryResult", {}).get("allRequiredParamsPresent", False)

    # Получаем chat_id
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
        orig = req.get("originalDetectIntentRequest", {}).get("payload", {})
        telegram_data = orig.get("data", {})
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

    logger.info(f"Intent: {intent_name}, ChatID: {chat_id}, Query: {query_text}")
    logger.info(f"Params: {json.dumps(params, ensure_ascii=False)}")
    logger.info(f"allRequiredParamsPresent: {all_required_present}")

    # --- GREETING / START ---
    if "privet" in intent_name or "start" in intent_name or "hello" in intent_name or "greeting" in intent_name or "welcome" in intent_name:
        return handle_greeting(chat_id)

    # --- REMINDER / EVENT ---
    if "napomin" in intent_name or "reminder" in intent_name or "sobytie" in intent_name or "event" in intent_name:
        return handle_reminder(params, query_text, chat_id, all_required_present)

    # --- OLYMPIAD ---
    elif "olimpiad" in intent_name or "olympiad" in intent_name:
        return handle_olympiad(params, query_text, chat_id, all_required_present)

    # --- CANCEL / DELETE ---
    elif "otmen" in intent_name or "cancel" in intent_name or "delete" in intent_name or "udal" in intent_name:
        return handle_cancel(params, query_text, chat_id)

    # --- UNKNOWN ---
    logger.info(f"Unknown intent: {intent_name}")
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

def handle_reminder(params, query_text, chat_id, all_required_present):
    """
    Обрабатывает создание события.
    Если all_required_present=False — значит Dialogflow ещё спрашивает параметры.
    Не создаём событие, а просто подтверждаем получение.
    """
    # Если Dialogflow ещё собирает параметры (slot filling) — не создаём событие
    if not all_required_present:
        logger.info("Slot filling in progress, not creating event yet")
        return jsonify({"fulfillmentText": ""})

    # Все параметры собраны — создаём событие
    name = get_param(params, "name")
    start_dt = extract_datetime(params, "date", "time")

    # Fallback: парсим из текста, если параметры кривые
    if not name or not start_dt:
        parsed_name, parsed_dt = parse_event_from_text(query_text)
        if parsed_name and not name:
            name = parsed_name
        if parsed_dt and not start_dt:
            start_dt = parsed_dt

    # Если всё ещё не хватает — ошибка
    if not name or not start_dt:
        logger.warning(f"Missing params after slot filling: name={name}, start_dt={start_dt}")
        return jsonify({"fulfillmentText": "Не удалось распознать данные. Попробуйте ещё раз."})

    # Парсим дату окончания из текста
    end_dt = None
    end_match = re.search(r"(?:по|до)\s+([0-9]{1,2}[./-][0-9]{1,2}[./-][0-9]{2,4}(?:\s+[0-9]{1,2}:[0-9]{2})?)", query_text, re.IGNORECASE)
    if end_match:
        end_dt = parse_from_text(end_match.group(1))

    if not end_dt:
        all_dates = re.findall(r"[0-9]{1,2}[./-][0-9]{1,2}[./-][0-9]{2,4}(?:\s+[0-9]{1,2}:[0-9]{2})?", query_text)
        if len(all_dates) >= 2:
            end_dt = parse_from_text(all_dates[1])

    # Fallback: end = start + 1 час
    if not end_dt:
        end_dt = start_dt + timedelta(hours=1)

    # Создаём событие
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
    logger.info(f"Reminder created: {response_text}")

    # Отправляем подтверждение в Telegram
    send_telegram_message(chat_id, f"✅ {response_text}")

    return jsonify({"fulfillmentText": response_text})

def handle_olympiad(params, query_text, chat_id, all_required_present):
    """
    Обрабатывает создание олимпиады.
    """
    # Если Dialogflow ещё собирает параметры — не создаём
    if not all_required_present:
        logger.info("Olympiad slot filling in progress")
        return jsonify({"fulfillmentText": ""})

    # Пробуем получить из параметров Dialogflow
    name = get_param(params, "any")
    subject = get_param(params, "subject")
    level = get_param(params, "level")
    start_date = get_param(params, "FROM")
    end_date = get_param(params, "to")

    start_dt = parse_date_time(start_date, None) if start_date else None
    end_dt = parse_date_time(end_date, None) if end_date else None

    # Fallback: парсим из текста
    if not name or not start_dt or not end_dt or not subject or not level:
        p_name, p_start, p_end, p_subj, p_lvl = parse_olympiad_from_text(query_text)
        if p_name and not name: name = p_name
        if p_start and not start_dt: start_dt = p_start
        if p_end and not end_dt: end_dt = p_end
        if p_subj and not subject: subject = p_subj
        if p_lvl and not level: level = p_lvl

    # Добавляем время по умолчанию
    if start_dt and start_dt.hour == 0 and start_dt.minute == 0:
        start_dt = start_dt.replace(hour=9, minute=0)
    if end_dt and end_dt.hour == 0 and end_dt.minute == 0:
        end_dt = end_dt.replace(hour=14, minute=0)

    # Проверяем, всё ли есть
    if not name or not start_dt or not end_dt:
        missing = []
        if not name: missing.append("название")
        if not start_dt: missing.append("дату начала")
        if not end_dt: missing.append("дату окончания")
        if not subject: missing.append("предмет")
        if not level: missing.append("уровень")

        logger.info(f"Olympiad missing: {missing}")
        return jsonify({"fulfillmentText": f"Укажите {', '.join(missing)} олимпиады."})

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
    logger.info(f"Olympiad created: {response_text}")

    send_telegram_message(chat_id, f"✅ {response_text}")

    return jsonify({"fulfillmentText": response_text})

def handle_cancel(params, query_text, chat_id):
    """
    Удаление события или олимпиады.
    """
    name = get_param(params, "any")
    date_val = get_param(params, "date")

    start_dt = parse_date_time(date_val, None) if date_val else None

    # Fallback: парсим из текста
    if not start_dt:
        all_dates = re.findall(r"[0-9]{1,2}[./-][0-9]{1,2}[./-][0-9]{2,4}(?:\s+[0-9]{1,2}:[0-9]{2})?", query_text)
        if all_dates:
            start_dt = parse_from_text(all_dates[0])

    if not name:
        # Пробуем вытащить название из текста
        cleaned = re.sub(r'^(отмени|отмена|отменить|удали|удалить|удаление)\s+', '', query_text, flags=re.IGNORECASE)
        name_match = re.search(r'^(.+?)\s+\d', cleaned)
        if name_match:
            name = name_match.group(1).strip()

    if not name:
        logger.warning("Cancel: no name provided")
        return jsonify({"fulfillmentText": "Укажите название события или олимпиады для удаления."})

    logger.info(f"Cancel search: name={name}, date={start_dt}, total_events={len(events)}")

    found = False
    for i, event in enumerate(events):
        if event["chat_id"] != chat_id:
            continue

        title_match = name.lower() in event["name"].lower() or event["name"].lower() in name.lower()

        date_match = True
        if start_dt:
            event_date = event["start_dt"].strftime("%d.%m.%Y")
            search_date = start_dt.strftime("%d.%m.%Y")
            date_match = event_date == search_date

        if title_match and date_match:
            remove_reminder_jobs(event["job_ids"])
            event_type = "событие" if event["event_type"] == "event" else "олимпиада"
            response_text = f"Удалил {event_type} «{event['name']}»"
            del events[i]
            logger.info(f"Cancelled: {response_text}")
            send_telegram_message(chat_id, f"🗑 {response_text}")
            return jsonify({"fulfillmentText": response_text})

    response_text = "Не нашел событие"
    logger.info(f"Cancel failed: {response_text} for name={name}")
    send_telegram_message(chat_id, f"❌ {response_text}")
    return jsonify({"fulfillmentText": response_text})

# ===================== HEALTH CHECK =====================
@app.route('/health', methods=['GET'])
def health():
    now_local = datetime.now(USER_TIMEZONE)
    now_utc = datetime.now(UTC)
    return jsonify({
        "status": "ok",
        "events_count": len(events),
        "scheduled_jobs": len(scheduled_jobs),
        "bot_token_set": BOT_TOKEN != "NOT_SET",
        "local_time": now_local.strftime("%d.%m.%Y %H:%M:%S %Z"),
        "utc_time": now_utc.strftime("%d.%m.%Y %H:%M:%S %Z")
    })

@app.route('/')
def index():
    return "Reminder Bot is running!"

# ===================== RUN =====================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
