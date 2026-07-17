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
USER_TIMEZONE = pytz.timezone('Asia/Yekaterinburg')
UTC = pytz.UTC
REMINDER_DELTAS = [168, 48, 24, 0]

# ===================== FILES =====================
DATA_FILE = "bot_data.json"
CHAT_ID_FILE = "chat_id.json"
SESSION_DATA_FILE = "session_data.json"

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
session_chat_ids = {}
session_data = {}  # session_id -> {name, date, time, step}
session_edit = {}  # session_id -> {event_index, field, step} для редактирования

# ===================== DATA PERSISTENCE =====================
def load_data()
load_session_edit():
    global events, session_chat_ids, session_data

    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                events = data.get("events", [])
                for ev in events:
                    if isinstance(ev.get("start_dt"), str):
                        ev["start_dt"] = datetime.fromisoformat(ev["start_dt"])
                    if isinstance(ev.get("end_dt"), str):
                        ev["end_dt"] = datetime.fromisoformat(ev["end_dt"])
                    if "sent_reminders" not in ev:
                        ev["sent_reminders"] = []
                logger.info(f"Loaded {len(events)} events from {DATA_FILE}")
        except Exception as e:
            logger.error(f"Failed to load data: {e}")

    if os.path.exists(CHAT_ID_FILE):
        try:
            with open(CHAT_ID_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                session_chat_ids = data.get("chat_ids", {})
                logger.info(f"Loaded {len(session_chat_ids)} chat_ids")
        except Exception as e:
            logger.error(f"Failed to load chat_ids: {e}")

    if os.path.exists(SESSION_DATA_FILE):
        try:
            with open(SESSION_DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                session_data = data.get("sessions", {})
                logger.info(f"Loaded {len(session_data)} session data entries")
        except Exception as e:
            logger.error(f"Failed to load session data: {e}")

def save_data():
    try:
        save_events = []
        for ev in events:
            save_ev = dict(ev)
            if isinstance(save_ev.get("start_dt"), datetime):
                save_ev["start_dt"] = save_ev["start_dt"].isoformat()
            if isinstance(save_ev.get("end_dt"), datetime):
                save_ev["end_dt"] = save_ev["end_dt"].isoformat()
            save_events.append(save_ev)

        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump({"events": save_events}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save data: {e}")

def save_chat_id_data():
    try:
        with open(CHAT_ID_FILE, 'w', encoding='utf-8') as f:
            json.dump({"chat_ids": session_chat_ids}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save chat_ids: {e}")

def save_session_data():
    try:
        with open(SESSION_DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump({"sessions": session_data}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save session data: {e}")

def save_session_edit():
    try:
        with open("session_edit.json", 'w', encoding='utf-8') as f:
            json.dump({"sessions": session_edit}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save session edit: {e}")

def load_session_edit():
    global session_edit
    if os.path.exists("session_edit.json"):
        try:
            with open("session_edit.json", 'r', encoding='utf-8') as f:
                data = json.load(f)
                session_edit = data.get("sessions", {})
        except Exception as e:
            logger.error(f"Failed to load session edit: {e}")

load_data()
load_session_edit()

# ===================== SCHEDULER =====================
scheduler = BackgroundScheduler(timezone=UTC)
scheduler.start()

# ===================== DATE/TIME PARSER =====================
def parse_date_time(date_str, time_str):
    if not date_str:
        return None
    date_str = str(date_str).strip()
    time_str = str(time_str).strip() if time_str else ""

    try:
        if 'T' in date_str:
            date_part = date_str.split('T')[0]
        else:
            date_part = date_str

        time_part = None
        if time_str:
            if 'T' in time_str:
                time_part = time_str.split('T')[1].split('+')[0].split('Z')[0]
            else:
                time_part = time_str

        if time_part:
            dt_str = f"{date_part}T{time_part}"
            dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S")
            dt = USER_TIMEZONE.localize(dt)
            return dt
        else:
            dt = datetime.strptime(date_part, "%Y-%m-%d")
            dt = USER_TIMEZONE.localize(dt)
            return dt

    except Exception as e:
        logger.error(f"Parse error: {e} for date={date_str}, time={time_str}")
    return None

def parse_from_text(text):
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
    if BOT_TOKEN == "NOT_SET":
        logger.error("BOT_TOKEN not set!")
        return False
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
        return r.status_code == 200
    except Exception as e:
        logger.error(f"Send error: {e}")
        return False

def get_main_keyboard():
    return {
        "keyboard": [
            [{"text": "👋 Привет"}],
            [{"text": "📅 Создать событие"}],
            [{"text": "🏆 Новая олимпиада"}],
            [{"text": "🗑 Удалить событие"}],
            [{"text": "🔄 Перенести событие"}],
            [{"text": "📋 Список событий"}],
            [{"text": "🧹 Очистить всё"}]
        ],
        "resize_keyboard": True
    }

# ===================== CHAT ID MANAGEMENT =====================
def get_chat_id(req, session_id):
    """Возвращает chat_id пользователя. Бот работает только для одного пользователя."""
    # Ваш chat_id — жёстко прописан
    MY_CHAT_ID = 5241670548
    return MY_CHAT_ID

def ask_for_chat_id():
    """Больше не используется — chat_id прописан жёстко."""
    return jsonify({"fulfillmentText": ""})

# ===================== SCHEDULER =====================
def schedule_reminders(chat_id, event_type, name, start_dt, end_dt, subject=None, level=None):
    job_ids = []
    now = datetime.now(USER_TIMEZONE)

    logger.info(f"Scheduling reminders. Now: {now}, Event start: {start_dt}")

    for delta_hours in REMINDER_DELTAS:
        reminder_time = start_dt - timedelta(hours=delta_hours)
        reminder_time_utc = reminder_time.astimezone(UTC)
        now_utc = now.astimezone(UTC)

        if reminder_time_utc <= now_utc:
            logger.info(f"Skipping reminder {delta_hours}h: time passed")
            continue

        job_id = f"{chat_id}_{name}_{start_dt.isoformat()}_{delta_hours}"

        hours_text = str(delta_hours) if delta_hours > 0 else "0"
        start_str = format_dt(start_dt)
        end_str = format_dt(end_dt)

        if event_type == "olympiad":
            message = (f"⏰ Напоминание! Через «{hours_text}» часов будет олимпиада "
                       f"«{name}» {subject} {level} уровня "
                       f"с {start_str} по {end_str}")
        else:
            message = (f"⏰ Напоминание! Через «{hours_text}» часов будет событие "
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
        logger.info(f"Scheduled reminder: {job_id} at {reminder_time_utc} UTC")

    return job_ids

def remove_reminder_jobs(job_ids):
    for job_id in job_ids:
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass
        if job_id in scheduled_jobs:
            del scheduled_jobs[job_id]

def check_and_send_overdue_reminders(chat_id):
    now = datetime.now(USER_TIMEZONE)
    sent = 0

    for event in events:
        if event["chat_id"] != chat_id:
            continue

        start_dt = event["start_dt"]
        if isinstance(start_dt, str):
            start_dt = datetime.fromisoformat(start_dt)

        if "sent_reminders" not in event:
            event["sent_reminders"] = []

        for delta_hours in REMINDER_DELTAS:
            reminder_time = start_dt - timedelta(hours=delta_hours)

            if delta_hours in event["sent_reminders"]:
                continue

            if now >= reminder_time:
                if now <= start_dt + timedelta(hours=2):
                    hours_text = str(delta_hours) if delta_hours > 0 else "0"
                    start_str = format_dt(start_dt)
                    end_dt = event["end_dt"]
                    if isinstance(end_dt, str):
                        end_dt = datetime.fromisoformat(end_dt)
                    end_str = format_dt(end_dt)

                    if event["event_type"] == "olympiad":
                        message = (f"⏰ Напоминание! Через «{hours_text}» часов будет олимпиада "
                                   f"«{event['name']}» {event.get('subject', '')} {event.get('level', '')} уровня "
                                   f"с {start_str} по {end_str}")
                    else:
                        message = (f"⏰ Напоминание! Через «{hours_text}» часов будет событие "
                                   f"«{event['name']}» с {start_str} по {end_str}")

                    if send_telegram_message(chat_id, message):
                        event["sent_reminders"].append(delta_hours)
                        sent += 1
                        logger.info(f"Sent overdue reminder: {delta_hours}h for {event['name']}")

    if sent > 0:
        save_data()

    return sent

# ===================== PARAM EXTRACTOR =====================
def get_param(params, name, default=None):
    val = params.get(name, default)
    if isinstance(val, dict):
        if "date_time" in val:
            return val["date_time"]
        return val.get("name") or val.get("value") or str(val)
    return val

def extract_datetime(params, date_key, time_key):
    d = get_param(params, date_key)
    t = get_param(params, time_key)
    if d:
        return parse_date_time(d, t)
    return None

# ===================== SLOT FILLING HELPERS =====================
def get_slot_filling_data(session_id, params, query_text, intent_name):
    """
    Собирает данные из slot filling пошагово.
    Сохраняет промежуточные данные в session_data.
    """
    # Инициализируем session_data если нет
    if session_id not in session_data:
        session_data[session_id] = {"step": 0, "name": None, "date": None, "time": None}

    sd = session_data[session_id]

    # Определяем, на каком шаге мы находимся
    # Шаг 0: только что начали, params пустые
    # Шаг 1: есть name, нет date
    # Шаг 2: есть name и date, нет time
    # Шаг 3: все параметры собраны

    name_param = get_param(params, "name")
    date_param = get_param(params, "date")
    time_param = get_param(params, "time")

    logger.info(f"Slot filling check: name={name_param}, date={date_param}, time={time_param}, step={sd['step']}")

    # Если name пришло и оно не похоже на дату/время — сохраняем
    if name_param and sd["step"] == 0:
        # Проверяем, что name не является датой или временем
        if not re.match(r'^\d{1,2}[.:]\d{2}$', str(name_param)) and not re.match(r'^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$', str(name_param)):
            sd["name"] = str(name_param)
            sd["step"] = 1
            logger.info(f"Saved name: {sd['name']}")

    # Если date пришло — сохраняем
    if date_param and sd["step"] >= 1:
        sd["date"] = str(date_param)
        sd["step"] = 2
        logger.info(f"Saved date: {sd['date']}")

    # Если time пришло — сохраняем
    if time_param and sd["step"] >= 2:
        sd["time"] = str(time_param)
        sd["step"] = 3
        logger.info(f"Saved time: {sd['time']}")

    save_session_data()
    return sd

def find_event_by_name(chat_id, name):
    """Находит событие по названию для данного пользователя."""
    for i, event in enumerate(events):
        if event["chat_id"] == chat_id:
            if name.lower() in event["name"].lower() or event["name"].lower() in name.lower():
                return i, event
    return None, None

def reset_session_data(session_id):
    """Сбрасывает данные сессии после создания события."""
    if session_id in session_data:
        session_data[session_id] = {"step": 0, "name": None, "date": None, "time": None}
        save_session_data()

# ===================== WEBHOOK =====================
@app.route('/render', methods=['POST'])
def render_webhook():
    return webhook()

@app.route('/webhook', methods=['POST'])
def webhook():
    req = request.get_json(silent=True, force=True)

    req_str = json.dumps(req, ensure_ascii=False)
    logger.info(f"=== WEBHOOK === {req_str[:800]}")

    intent_name = req.get("queryResult", {}).get("intent", {}).get("displayName", "").lower()
    params = req.get("queryResult", {}).get("parameters", {})
    query_text = req.get("queryResult", {}).get("queryText", "")
    all_required_present = req.get("queryResult", {}).get("allRequiredParamsPresent", False)
    session = req.get("session", "")

    chat_id = get_chat_id(req, session)
    logger.info(f"Using chat_id: {chat_id}")

    logger.info(f"Intent: {intent_name}, ChatID: {chat_id}, Query: {query_text}")
    logger.info(f"Params: {json.dumps(params, ensure_ascii=False)}")
    logger.info(f"allRequiredParamsPresent: {all_required_present}")

    # Проверяем просроченные напоминания
    overdue_sent = check_and_send_overdue_reminders(chat_id)
    if overdue_sent > 0:
        logger.info(f"Sent {overdue_sent} overdue reminders")

    # --- GREETING / START ---
    if any(word in intent_name for word in ["privet", "start", "hello", "greeting", "welcome", "poka"]):
        return handle_greeting(chat_id)

    # --- REMINDER / EVENT ---
    if any(word in intent_name for word in ["napomin", "reminder", "sobytie", "event", "sozdat", "dobavit"]):
        return handle_reminder(params, query_text, chat_id, all_required_present, session)

    # --- OLYMPIAD ---
    elif any(word in intent_name for word in ["olimpiad", "olympiad"]):
        return handle_olympiad(params, query_text, chat_id, all_required_present, session)

    # --- CANCEL / DELETE ---
    elif any(word in intent_name for word in ["otmen", "cancel", "delete", "udal", "ubrat"]):
        return handle_cancel(params, query_text, chat_id)

    # --- LIST ---
    elif any(word in intent_name for word in ["spisok", "list", "pokaz", "show", "vse", "vse sobytiya"]):
        return handle_list(chat_id)

    # --- CLEAR ALL ---
    elif any(word in intent_name for word in ["ochistit", "clear", "udalit vse", "ubrat vse"]):
        return handle_clear_all(chat_id)

    # --- RESCHEDULE / PERENESTI ---
    elif any(word in intent_name for word in ["perenesti", "reschedule", "perenosit", "pomenuat", "izmenit vremya"]):
        return handle_reschedule(params, query_text, chat_id, all_required_present, session)

    # --- UNKNOWN ---
    logger.info(f"Unknown intent: {intent_name}")
    return jsonify({"fulfillmentText": ""})

# ===================== HANDLERS =====================

def handle_greeting(chat_id):
    welcome_text = (
        "👋 Привет! Я бот для напоминаний!\n\n"
        "📌 <b>Команды:</b>\n"
        "• 📅 Создать событие — напишите: создать событие\n"
        "• 🏆 Новая олимпиада — напишите: олимпиада\n"
        "• 🗑 Удалить событие — напишите: удалить Название дд.мм.гггг\n"
        "• 📋 Список событий\n"
        "• 🧹 Очистить всё\n\n"
        "Напоминания приходят за 7 дней, 2 дня, 1 день и в момент события!"
    )
    send_telegram_message(chat_id, welcome_text, get_main_keyboard())
    return jsonify({"fulfillmentText": ""})

def handle_reminder(params, query_text, chat_id, all_required_present, session_id):
    """Обрабатывает создание события с slot filling."""

    # Собираем данные из slot filling
    sd = get_slot_filling_data(session_id, params, query_text, "event")

    # Если Dialogflow ещё собирает параметры — не создаём событие
    if not all_required_present:
        logger.info(f"Slot filling step {sd['step']}: name={sd['name']}, date={sd['date']}, time={sd['time']}")
        return jsonify({"fulfillmentText": ""})

    # Все параметры собраны — используем сохранённые данные из session_data
    # Название берём из session_data (первый ответ), а не из params (последний ответ)
    name = sd.get("name")
    date_str = sd.get("date")
    time_str = sd.get("time")

    # Fallback: если в session_data нет — берём из params
    if not name:
        name = get_param(params, "name")
    if not date_str:
        date_str = get_param(params, "date")
    if not time_str:
        time_str = get_param(params, "time")

    # Парсим дату и время
    start_dt = None
    if date_str and time_str:
        start_dt = parse_date_time(date_str, time_str)
    elif date_str:
        start_dt = parse_date_time(date_str, None)

    # Fallback: парсим из текста
    if not start_dt:
        parsed_name, parsed_dt = parse_event_from_text(query_text)
        if parsed_dt:
            start_dt = parsed_dt

    # Если всё ещё не хватает — ошибка
    if not name or not start_dt:
        logger.warning(f"Missing data: name={name}, start_dt={start_dt}")
        reset_session_data(session_id)
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

    if not end_dt:
        end_dt = start_dt + timedelta(hours=1)

    # Создаём событие
    job_ids = schedule_reminders(chat_id, "event", name, start_dt, end_dt)

    event_data = {
        "chat_id": chat_id,
        "event_type": "event",
        "name": name,
        "start_dt": start_dt,
        "end_dt": end_dt,
        "subject": None,
        "level": None,
        "job_ids": job_ids,
        "sent_reminders": [],
    }
    events.append(event_data)
    save_data()

    response_text = f"Добавлено новое событие «{name}» с {format_dt(start_dt)} по {format_dt(end_dt)}"
    logger.info(f"Event created: {response_text}")

    send_telegram_message(chat_id, f"✅ {response_text}")

    # Сбрасываем session_data
    reset_session_data(session_id)

    return jsonify({"fulfillmentText": ""})

def handle_olympiad(params, query_text, chat_id, all_required_present, session_id):
    """Обрабатывает создание олимпиады. ВСЕГДА парсит из queryText для точности."""

    # ВСЕГДА парсим из текста пользователя — так надёжнее Dialogflow params
    p_name, p_start, p_end, p_subj, p_lvl = parse_olympiad_from_text(query_text)

    # Fallback: берём из params если из текста не получилось
    name = p_name or get_param(params, "any") or "Олимпиада"
    subject = p_subj or get_param(params, "subject") or ""
    level = p_lvl or get_param(params, "level") or "1"
    start_dt = p_start
    end_dt = p_end

    # Если из текста не получилось даты — пробуем из params
    if not start_dt:
        start_date = get_param(params, "FROM")
        if start_date:
            start_dt = parse_date_time(start_date, None)
    if not end_dt:
        end_date = get_param(params, "to")
        if end_date:
            end_dt = parse_date_time(end_date, None)

    # Если время не указано (только дата) — ставим дефолтное
    if start_dt and start_dt.hour == 0 and start_dt.minute == 0:
        start_dt = start_dt.replace(hour=9, minute=0)
    if end_dt and end_dt.hour == 0 and end_dt.minute == 0:
        end_dt = end_dt.replace(hour=14, minute=0)

    if not start_dt or not end_dt:
        missing = []
        if not start_dt: missing.append("дату начала")
        if not end_dt: missing.append("дату окончания")
        return jsonify({"fulfillmentText": f"Укажите {', '.join(missing)} олимпиады."})

    job_ids = schedule_reminders(chat_id, "olympiad", name, start_dt, end_dt, subject, level)

    event_data = {
        "chat_id": chat_id,
        "event_type": "olympiad",
        "name": name,
        "start_dt": start_dt,
        "end_dt": end_dt,
        "subject": subject,
        "level": level,
        "job_ids": job_ids,
        "sent_reminders": [],
    }
    events.append(event_data)
    save_data()

    response_text = f"Добавлена новая олимпиада «{name}» {subject} {level} уровня с {format_dt(start_dt)} по {format_dt(end_dt)}"
    logger.info(f"Olympiad created: {response_text}")

    send_telegram_message(chat_id, f"✅ {response_text}")

    return jsonify({"fulfillmentText": ""})

def handle_cancel(params, query_text, chat_id):
    name = get_param(params, "any")
    date_val = get_param(params, "date")

    start_dt = parse_date_time(date_val, None) if date_val else None

    if not start_dt:
        all_dates = re.findall(r"[0-9]{1,2}[./-][0-9]{1,2}[./-][0-9]{2,4}(?:\s+[0-9]{1,2}:[0-9]{2})?", query_text)
        if all_dates:
            start_dt = parse_from_text(all_dates[0])

    if not name:
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
            event_date = event["start_dt"].strftime("%d.%m.%Y") if isinstance(event["start_dt"], datetime) else datetime.fromisoformat(event["start_dt"]).strftime("%d.%m.%Y")
            search_date = start_dt.strftime("%d.%m.%Y")
            date_match = event_date == search_date

        if title_match and date_match:
            remove_reminder_jobs(event["job_ids"])
            event_type = "событие" if event["event_type"] == "event" else "олимпиада"
            response_text = f"Удалил {event_type} «{event['name']}»"
            del events[i]
            save_data()
            logger.info(f"Cancelled: {response_text}")
            send_telegram_message(chat_id, f"🗑 {response_text}")
            return jsonify({"fulfillmentText": ""})

    response_text = "Не нашёл событие"
    logger.info(f"Cancel failed: {response_text} for name={name}")
    send_telegram_message(chat_id, f"❌ {response_text}")
    return jsonify({"fulfillmentText": ""})

def handle_list(chat_id):
    user_events = [e for e in events if e["chat_id"] == chat_id]

    if not user_events:
        send_telegram_message(chat_id, "📭 У вас пока нет событий.")
        return jsonify({"fulfillmentText": ""})

    user_events.sort(key=lambda x: x["start_dt"] if isinstance(x["start_dt"], datetime) else datetime.fromisoformat(x["start_dt"]))

    text = "📋 <b>Ваши события:</b>\n\n"
    for i, ev in enumerate(user_events, 1):
        start = format_dt(ev["start_dt"]) if isinstance(ev["start_dt"], datetime) else format_dt(datetime.fromisoformat(ev["start_dt"]))
        end = format_dt(ev["end_dt"]) if isinstance(ev["end_dt"], datetime) else format_dt(datetime.fromisoformat(ev["end_dt"]))

        if ev["event_type"] == "olympiad":
            text += f"{i}. 🏆 <b>{ev['name']}</b> ({ev.get('subject', '')} {ev.get('level', '')} ур.)\n   📅 {start} — {end}\n\n"
        else:
            text += f"{i}. 📅 <b>{ev['name']}</b>\n   🕐 {start} — {end}\n\n"

    send_telegram_message(chat_id, text)
    return jsonify({"fulfillmentText": ""})

def handle_clear_all(chat_id):
    global events
    user_events = [e for e in events if e["chat_id"] == chat_id]

    for event in user_events:
        remove_reminder_jobs(event["job_ids"])

    events = [e for e in events if e["chat_id"] != chat_id]
    save_data()

    send_telegram_message(chat_id, "🧹 Все события удалены!")
    return jsonify({"fulfillmentText": ""})

def handle_edit(params, query_text, chat_id, session_id):
    """Обрабатывает редактирование события/олимпиады."""

    # Инициализируем или получаем текущее состояние редактирования
    if session_id not in session_edit:
        session_edit[session_id] = {"step": "ask_name", "event_index": None, "field": None}

    se = session_edit[session_id]

    # Шаг 1: Спрашиваем название события
    if se["step"] == "ask_name":
        # Проверяем, не указал ли пользователь название сразу
        name = get_param(params, "any") or query_text.strip()

        # Ищем событие
        idx, event = find_event_by_name(chat_id, name)

        if idx is None:
            send_telegram_message(chat_id, "❌ Не нашёл событие. Попробуйте изменить снова.")
            del session_edit[session_id]
            save_session_edit()
            return jsonify({"fulfillmentText": ""})

        se["event_index"] = idx
        se["step"] = "ask_field"
        save_session_edit()

        event_type = "событие" if event["event_type"] == "event" else "олимпиада"
        start_str = format_dt(event["start_dt"]) if isinstance(event["start_dt"], datetime) else format_dt(datetime.fromisoformat(event["start_dt"]))
        end_str = format_dt(event["end_dt"]) if isinstance(event["end_dt"], datetime) else format_dt(datetime.fromisoformat(event["end_dt"]))

        # Формируем список доступных полей
        if event["event_type"] == "event":
            fields_text = "название, дата начала, время начала, дата конца, время конца"
        else:
            fields_text = "название, дата начала, время начала, дата конца, время конца, предмет, уровень"

        send_telegram_message(chat_id, 
            f"✏️ Найдено {event_type} «{event['name']}»\n"
            f"📅 {start_str} — {end_str}\n\n"
            f"Что вы хотите изменить? ({fields_text})")
        return jsonify({"fulfillmentText": ""})

    # Шаг 2: Пользователь выбирает поле для изменения
    elif se["step"] == "ask_field":
        field_map = {
            "название": "name", "имя": "name", "name": "name",
            "дата начала": "start_date", "дата": "start_date", "date": "start_date",
            "время начала": "start_time", "время": "start_time", "time": "start_time",
            "дата конца": "end_date", "конец дата": "end_date",
            "время конца": "end_time", "конец время": "end_time",
            "предмет": "subject", "subject": "subject",
            "уровень": "level", "level": "level"
        }

        field = None
        query_lower = query_text.lower()
        for key, val in field_map.items():
            if key in query_lower:
                field = val
                break

        if not field:
            send_telegram_message(chat_id, "Не понял, что изменить. Попробуйте: название, дата, время, предмет или уровень.")
            return jsonify({"fulfillmentText": ""})

        se["field"] = field
        se["step"] = "ask_value"
        save_session_edit()

        # Спрашиваем новое значение
        field_names = {
            "name": "название", "start_date": "дату начала", "start_time": "время начала",
            "end_date": "дату конца", "end_time": "время конца", "subject": "предмет", "level": "уровень"
        }
        send_telegram_message(chat_id, f"Введите новое {field_names.get(field, field)}:")
        return jsonify({"fulfillmentText": ""})

    # Шаг 3: Пользователь вводит новое значение
    elif se["step"] == "ask_value":
        idx = se["event_index"]
        field = se["field"]
        event = events[idx]

        # Применяем изменение
        success = apply_edit(event, field, query_text)

        if not success:
            send_telegram_message(chat_id, "❌ Не удалось распознать значение. Попробуйте ещё раз.")
            return jsonify({"fulfillmentText": ""})

        # Пересоздаём напоминания
        remove_reminder_jobs(event["job_ids"])

        start_dt = event["start_dt"]
        end_dt = event["end_dt"]
        if isinstance(start_dt, str):
            start_dt = datetime.fromisoformat(start_dt)
        if isinstance(end_dt, str):
            end_dt = datetime.fromisoformat(end_dt)

        new_job_ids = schedule_reminders(
            chat_id, 
            event["event_type"], 
            event["name"], 
            start_dt, 
            end_dt,
            event.get("subject"),
            event.get("level")
        )
        event["job_ids"] = new_job_ids
        event["sent_reminders"] = []

        save_data()

        # Отправляем подтверждение
        start_str = format_dt(start_dt)
        end_str = format_dt(end_dt)

        if event["event_type"] == "olympiad":
            send_telegram_message(chat_id, 
                f"✅ Олимпиада изменена!\n"
                f"«{event['name']}» {event.get('subject', '')} {event.get('level', '')} уровня\n"
                f"📅 {start_str} — {end_str}")
        else:
            send_telegram_message(chat_id, 
                f"✅ Событие изменено!\n"
                f"«{event['name']}»\n"
                f"📅 {start_str} — {end_str}")

        # Сбрасываем состояние
        del session_edit[session_id]
        save_session_edit()

        return jsonify({"fulfillmentText": ""})

    return jsonify({"fulfillmentText": ""})

def apply_edit(event, field, value):
    """Применяет изменение к событию."""
    try:
        if field == "name":
            event["name"] = value.strip()
            return True

        elif field == "start_date":
            new_date = parse_date_time(value, None)
            if new_date:
                old_time = event["start_dt"]
                if isinstance(old_time, str):
                    old_time = datetime.fromisoformat(old_time)
                event["start_dt"] = new_date.replace(hour=old_time.hour, minute=old_time.minute)
                return True
            return False

        elif field == "start_time":
            time_match = re.search(r'(\d{1,2}):(\d{2})', value)
            if time_match:
                h, m = int(time_match.group(1)), int(time_match.group(2))
                dt = event["start_dt"]
                if isinstance(dt, str):
                    dt = datetime.fromisoformat(dt)
                event["start_dt"] = dt.replace(hour=h, minute=m)
                return True
            return False

        elif field == "end_date":
            new_date = parse_date_time(value, None)
            if new_date:
                old_time = event["end_dt"]
                if isinstance(old_time, str):
                    old_time = datetime.fromisoformat(old_time)
                event["end_dt"] = new_date.replace(hour=old_time.hour, minute=old_time.minute)
                return True
            return False

        elif field == "end_time":
            time_match = re.search(r'(\d{1,2}):(\d{2})', value)
            if time_match:
                h, m = int(time_match.group(1)), int(time_match.group(2))
                dt = event["end_dt"]
                if isinstance(dt, str):
                    dt = datetime.fromisoformat(dt)
                event["end_dt"] = dt.replace(hour=h, minute=m)
                return True
            return False

        elif field == "subject":
            event["subject"] = value.strip()
            return True

        elif field == "level":
            if value.strip() in ['1', '2', '3']:
                event["level"] = value.strip()
                return True
            return False

        return False
    except Exception as e:
        logger.error(f"Apply edit error: {e}")
        return False

def handle_reschedule(params, query_text, chat_id, all_required_present, session_id):
    """Переносит событие или олимпиаду на новое время."""

    # Парсим из текста: "перенести Название 17.07.2026 11:06 на 18.07.2026 12:00"
    # Или: "перенести Название 17.07.2026 на 18.07.2026"

    # Убираем слово "перенести" и варианты
    cleaned = re.sub(r'^(перенести|перенеси|поменять|изменить|сдвинуть|reschedule)\s+', '', query_text, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+(перенести|перенеси|поменять|изменить|сдвинуть|reschedule)\s+', ' ', cleaned, flags=re.IGNORECASE)

    # Ищем ключевое слово "на" — разделитель старой и новой даты
    parts = cleaned.split(' на ')
    if len(parts) < 2:
        parts = cleaned.split(' на ')

    if len(parts) < 2:
        # Пробуем найти "на" без пробелов или другие варианты
        match = re.search(r'(.+?)\s+(на|->|→)\s+(.+)', cleaned)
        if match:
            old_part = match.group(1).strip()
            new_part = match.group(3).strip()
        else:
            send_telegram_message(chat_id, "❌ Формат: перенести Название дд.мм.гггг чч:мм на дд.мм.гггг чч:мм")
            return jsonify({"fulfillmentText": ""})
    else:
        old_part = parts[0].strip()
        new_part = parts[1].strip()

    logger.info(f"Reschedule: old='{old_part}', new='{new_part}'")

    # Парсим старую дату из old_part
    old_date_match = re.search(r'(\d{1,2}\.\d{1,2}\.\d{4}(?:\s+\d{1,2}:\d{2})?)', old_part)
    old_date_str = old_date_match.group(1) if old_date_match else None
    old_dt = parse_from_text(old_date_str) if old_date_str else None

    # Название — всё до даты
    name = old_part
    if old_date_match:
        name = old_part[:old_date_match.start()].strip()
    # Убираем лишние слова из начала
    name = re.sub(r'^(событие|олимпиада|напоминание)\s+', '', name, flags=re.IGNORECASE).strip()

    # Парсим новую дату из new_part
    new_date_match = re.search(r'(\d{1,2}\.\d{1,2}\.\d{4}(?:\s+\d{1,2}:\d{2})?)', new_part)
    new_date_str = new_date_match.group(1) if new_date_match else None
    new_dt = parse_from_text(new_date_str) if new_date_str else None

    if not name:
        send_telegram_message(chat_id, "❌ Укажите название события.")
        return jsonify({"fulfillmentText": ""})

    if not old_dt:
        send_telegram_message(chat_id, "❌ Укажите текущую дату события.")
        return jsonify({"fulfillmentText": ""})

    if not new_dt:
        send_telegram_message(chat_id, "❌ Укажите новую дату.")
        return jsonify({"fulfillmentText": ""})

    # Ищем событие
    found = False
    for i, event in enumerate(events):
        if event["chat_id"] != chat_id:
            continue

        title_match = name.lower() in event["name"].lower() or event["name"].lower() in name.lower()

        event_dt = event["start_dt"]
        if isinstance(event_dt, str):
            event_dt = datetime.fromisoformat(event_dt)

        date_match = (event_dt.strftime("%d.%m.%Y") == old_dt.strftime("%d.%m.%Y"))

        if title_match and date_match:
            # Удаляем старые напоминания
            remove_reminder_jobs(event["job_ids"])

            # Обновляем время
            old_start = event["start_dt"]
            if isinstance(old_start, str):
                old_start = datetime.fromisoformat(old_start)
            old_end = event["end_dt"]
            if isinstance(old_end, str):
                old_end = datetime.fromisoformat(old_end)

            # Считаем разницу между старым началом и концом
            duration = old_end - old_start

            event["start_dt"] = new_dt
            event["end_dt"] = new_dt + duration
            event["sent_reminders"] = []  # Сбрасываем отправленные напоминания

            # Создаём новые напоминания
            job_ids = schedule_reminders(
                chat_id,
                event["event_type"],
                event["name"],
                event["start_dt"],
                event["end_dt"],
                event.get("subject"),
                event.get("level")
            )
            event["job_ids"] = job_ids

            save_data()

            event_type = "событие" if event["event_type"] == "event" else "олимпиада"
            response_text = (f"Перенёс {event_type} «{event['name']}»\n"
                           f"С {format_dt(old_start)} на {format_dt(new_dt)}")

            logger.info(f"Rescheduled: {response_text}")
            send_telegram_message(chat_id, f"✅ {response_text}")
            return jsonify({"fulfillmentText": ""})

    send_telegram_message(chat_id, f"❌ Не нашёл событие «{name}» на {format_dt(old_dt)}")
    return jsonify({"fulfillmentText": ""})

# ===================== HEALTH CHECK =====================
@app.route('/health', methods=['GET'])
def health():
    now_local = datetime.now(USER_TIMEZONE)
    now_utc = datetime.now(UTC)

    # Проверяем напоминания для вашего chat_id при каждом /health запросе
    # Это нужно для Render free tier — сервер засыпает, но cron-job.org будит его
    MY_CHAT_ID = 5241670548
    total_sent = 0
    try:
        sent = check_and_send_overdue_reminders(MY_CHAT_ID)
        total_sent += sent
    except Exception as e:
        logger.error(f"Error checking reminders: {e}")

    if total_sent > 0:
        logger.info(f"Health check sent {total_sent} overdue reminders")

    return jsonify({
        "status": "ok",
        "events_count": len(events),
        "scheduled_jobs": len(scheduled_jobs),
        "bot_token_set": BOT_TOKEN != "NOT_SET",
        "local_time": now_local.strftime("%d.%m.%Y %H:%M:%S %Z"),
        "utc_time": now_utc.strftime("%d.%m.%Y %H:%M:%S %Z"),
        "reminders_sent": total_sent
    })

@app.route('/')
def index():
    return "Reminder Bot is running!"

# ===================== RUN =====================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
