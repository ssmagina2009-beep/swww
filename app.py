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

# ===================== DATA PERSISTENCE =====================
def load_data():
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

load_data()

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
            [{"text": "📋 Список событий"}],
            [{"text": "🧹 Очистить всё"}]
        ],
        "resize_keyboard": True
    }

# ===================== CHAT ID MANAGEMENT =====================
def get_chat_id(req, session_id):
    if session_id in session_chat_ids:
        return session_chat_ids[session_id]

    output_contexts = req.get("queryResult", {}).get("outputContexts", [])
    for ctx in output_contexts:
        ctx_params = ctx.get("parameters", {})
        for key in ["chat_id", "chatId", "from_id", "telegram_chat_id"]:
            if key in ctx_params and ctx_params[key]:
                try:
                    cid = int(ctx_params[key])
                    session_chat_ids[session_id] = cid
                    save_chat_id_data()
                    return cid
                except:
                    pass

    orig = req.get("originalDetectIntentRequest", {})
    payload = orig.get("payload", {})
    telegram_data = payload.get("data", {})

    if "message" in telegram_data:
        chat_id = telegram_data["message"]["chat"]["id"]
        session_chat_ids[session_id] = chat_id
        save_chat_id_data()
        return chat_id
    elif "callback_query" in telegram_data:
        chat_id = telegram_data["callback_query"]["message"]["chat"]["id"]
        session_chat_ids[session_id] = chat_id
        save_chat_id_data()
        return chat_id

    query_text = req.get("queryResult", {}).get("queryText", "")
    chat_id_match = re.search(r'(?:мой\s+chat[-_]?id|chat[-_]?id|id)\s*[:=]?\s*(\d+)', query_text, re.IGNORECASE)
    if chat_id_match:
        cid = int(chat_id_match.group(1))
        session_chat_ids[session_id] = cid
        save_chat_id_data()
        return cid

    return None

def ask_for_chat_id():
    return jsonify({
        "fulfillmentText": (
            "🔑 Для работы мне нужен ваш chat_id из Telegram.\n\n"
            "Как получить:\n"
            "1. Откройте Telegram\n"
            "2. Найдите бота @userinfobot\n"
            "3. Напишите ему что-нибудь\n"
            "4. Он пришлёт ваш ID (число, например 123456789)\n\n"
            "Отправьте мне: <b>мой chat_id 123456789</b>"
        )
    })

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

    if not chat_id:
        logger.warning(f"No chat_id found for session {session}, asking user")
        return ask_for_chat_id()

    try:
        chat_id = int(chat_id)
    except:
        logger.error(f"Invalid chat_id: {chat_id}")
        return ask_for_chat_id()

    logger.info(f"Intent: {intent_name}, ChatID: {chat_id}, Query: {query_text}")
    logger.info(f"Params: {json.dumps(params, ensure_ascii=False)}")
    logger.info(f"allRequiredParamsPresent: {all_required_present}")

    # Проверяем просроченные напоминания
    overdue_sent = check_and_send_overdue_reminders(chat_id)
    if overdue_sent > 0:
        logger.info(f"Sent {overdue_sent} overdue reminders")

    # --- CHAT ID SETUP ---
    if "chat_id" in intent_name or "chatid" in intent_name or re.search(r'chat[-_]?id', query_text, re.IGNORECASE):
        match = re.search(r'(\d{7,})', query_text)
        if match:
            new_chat_id = int(match.group(1))
            session_chat_ids[session] = new_chat_id
            save_chat_id_data()
            send_telegram_message(new_chat_id, "✅ Chat_id сохранён! Теперь вы можете создавать события.")
            return jsonify({"fulfillmentText": ""})
        else:
            return ask_for_chat_id()

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
    """Обрабатывает создание олимпиады с slot filling."""

    sd = get_slot_filling_data(session_id, params, query_text, "olympiad")

    if not all_required_present:
        logger.info(f"Olympiad slot filling step {sd['step']}")
        return jsonify({"fulfillmentText": ""})

    # Используем сохранённые данные
    name = sd.get("name") or get_param(params, "any")
    subject = get_param(params, "subject")
    level = get_param(params, "level")
    start_date = sd.get("date") or get_param(params, "FROM")
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

    if start_dt and start_dt.hour == 0 and start_dt.minute == 0:
        start_dt = start_dt.replace(hour=9, minute=0)
    if end_dt and end_dt.hour == 0 and end_dt.minute == 0:
        end_dt = end_dt.replace(hour=14, minute=0)

    if not name or not start_dt or not end_dt:
        missing = []
        if not name: missing.append("название")
        if not start_dt: missing.append("дату начала")
        if not end_dt: missing.append("дату окончания")
        if not subject: missing.append("предмет")
        if not level: missing.append("уровень")

        reset_session_data(session_id)
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
    reset_session_data(session_id)

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

# ===================== HEALTH CHECK =====================
@app.route('/health', methods=['GET'])
def health():
    now_local = datetime.now(USER_TIMEZONE)
    now_utc = datetime.now(UTC)

    # Проверяем напоминания для ВСЕХ пользователей при каждом /health запросе
    # Это нужно для Render free tier — сервер засыпает, но cron-job.org будит его
    total_sent = 0
    for chat_id in list(session_chat_ids.values()):
        try:
            sent = check_and_send_overdue_reminders(int(chat_id))
            total_sent += sent
        except Exception as e:
            logger.error(f"Error checking reminders for {chat_id}: {e}")

    if total_sent > 0:
        logger.info(f"Health check sent {total_sent} overdue reminders")

    return jsonify({
        "status": "ok",
        "events_count": len(events),
        "scheduled_jobs": len(scheduled_jobs),
        "chat_ids_saved": len(session_chat_ids),
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
