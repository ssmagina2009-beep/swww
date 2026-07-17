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

# Часовой пояс пользователя (UTC+5 — Екатеринбург)
USER_TIMEZONE = pytz.timezone('Asia/Yekaterinburg')  # UTC+5
UTC = pytz.UTC

# Reminder times (hours before event)
REMINDER_DELTAS = [168, 48, 24, 0]

# ===================== FILES =====================
DATA_FILE = "bot_data.json"
CHAT_ID_FILE = "chat_id.json"

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
session_chat_ids = {}  # session_id -> chat_id (временное, пока не сохранено)

# ===================== DATA PERSISTENCE =====================
def load_data():
    """Загружает события и chat_id из файлов."""
    global events, session_chat_ids
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                events = data.get("events", [])
                # Конвертируем строки datetime обратно в объекты
                for ev in events:
                    if isinstance(ev.get("start_dt"), str):
                        ev["start_dt"] = datetime.fromisoformat(ev["start_dt"])
                    if isinstance(ev.get("end_dt"), str):
                        ev["end_dt"] = datetime.fromisoformat(ev["end_dt"])
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

def save_data():
    """Сохраняет события в файл."""
    try:
        # Конвертируем datetime в строки для JSON
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
    """Сохраняет chat_ids в файл."""
    try:
        with open(CHAT_ID_FILE, 'w', encoding='utf-8') as f:
            json.dump({"chat_ids": session_chat_ids}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save chat_ids: {e}")

# Загружаем данные при старте
load_data()

# ===================== SCHEDULER =====================
scheduler = BackgroundScheduler(timezone=UTC)
scheduler.start()

# ===================== DATE/TIME PARSER =====================
def parse_date_time(date_str, time_str):
    """Собирает datetime из date и time от Dialogflow с учётом часового пояса."""
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
        logger.info(f"Telegram API response: {r.status_code}, body: {r.text[:200]}")
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
            [{"text": "🗑 Удалить событие"}]
        ],
        "resize_keyboard": True
    }

# ===================== CHAT ID MANAGEMENT =====================
def get_chat_id(req, session_id):
    """
    Получает chat_id из запроса Dialogflow.
    Проверяет: outputContexts -> originalDetectIntentRequest -> session_chat_ids -> session_id
    """
    # 1. Проверяем сохранённый chat_id для этой сессии
    if session_id in session_chat_ids:
        chat_id = session_chat_ids[session_id]
        logger.info(f"Found saved chat_id for session: {chat_id}")
        return chat_id

    # 2. Проверяем outputContexts (telegram контекст)
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

    # 3. Проверяем originalDetectIntentRequest (данные Telegram)
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

    # 4. Проверяем, ввёл ли пользователь chat_id в тексте сообщения
    query_text = req.get("queryResult", {}).get("queryText", "")
    chat_id_match = re.search(r'(?:мой\s+chat[-_]?id|chat[-_]?id|id)\s*[:=]?\s*(\d+)', query_text, re.IGNORECASE)
    if chat_id_match:
        cid = int(chat_id_match.group(1))
        session_chat_ids[session_id] = cid
        save_chat_id_data()
        logger.info(f"Extracted chat_id from text: {cid}")
        return cid

    return None

def ask_for_chat_id():
    """Возвращает ответ с просьбой ввести chat_id."""
    return jsonify({
        "fulfillmentText": (
            "🔑 Для работы мне нужен ваш chat_id из Telegram.\n\n"
            "Как получить:\n"
            "1. Откройте Telegram\n"
            "2. Найдите бота @userinfobot\n"
            "3. Напишите ему что-нибудь\n"
            "4. Он пришлёт ваш ID (число, например 123456789)\n\n"
            "Отправьте мне: <b>мой chat_id 123456789</b>"
        ),
        "fulfillmentMessages": [{
            "platform": "TELEGRAM",
            "payload": {
                "telegram": {
                    "text": (
                        "🔑 Для работы мне нужен ваш chat_id из Telegram.\n\n"
                        "Как получить:\n"
                        "1. Откройте Telegram\n"
                        "2. Найдите бота @userinfobot\n"
                        "3. Напишите ему что-нибудь\n"
                        "4. Он пришлёт ваш ID (число, например 123456789)\n\n"
                        "Отправьте мне: <b>мой chat_id 123456789</b>"
                    ),
                    "parse_mode": "HTML"
                }
            }
        }]
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
    """Удаление запланированных задач."""
    for job_id in job_ids:
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass
        if job_id in scheduled_jobs:
            del scheduled_jobs[job_id]

def check_and_send_overdue_reminders(chat_id):
    """
    Проверяет просроченные напоминания и отправляет их.
    Используется при каждом запросе, чтобы не пропустить напоминания,
    если сервер был выключен (Render free tier).
    """
    now = datetime.now(USER_TIMEZONE)
    sent = 0

    for event in events:
        if event["chat_id"] != chat_id:
            continue

        start_dt = event["start_dt"]
        if isinstance(start_dt, str):
            start_dt = datetime.fromisoformat(start_dt)

        # Проверяем напоминания для этого события
        for delta_hours in REMINDER_DELTAS:
            reminder_time = start_dt - timedelta(hours=delta_hours)

            # Если напоминание должно было сработать, но ещё не отправлено
            # (проверяем, что событие ещё не прошло или прошло недавно)
            if now >= reminder_time and now <= start_dt + timedelta(hours=1):
                # Проверяем, не отправляли ли уже это напоминание
                job_id = f"{chat_id}_{event['name']}_{start_dt.isoformat()}_{delta_hours}"

                # Если задача ещё не запланирована (сервер перезапускался)
                if job_id not in scheduled_jobs:
                    hours_text = str(delta_hours) if delta_hours > 0 else "0"
                    start_str = format_dt(start_dt)
                    end_str = format_dt(event["end_dt"] if isinstance(event["end_dt"], datetime) else datetime.fromisoformat(event["end_dt"]))

                    if event["event_type"] == "olympiad":
                        message = (f"⏰ Напоминание! Через «{hours_text}» часов будет олимпиада "
                                   f"«{event['name']}» {event.get('subject', '')} {event.get('level', '')} уровня "
                                   f"с {start_str} по {end_str}")
                    else:
                        message = (f"⏰ Напоминание! Через «{hours_text}» часов будет событие "
                                   f"«{event['name']}» с {start_str} по {end_str}")

                    send_telegram_message(chat_id, message)
                    sent += 1
                    logger.info(f"Sent overdue reminder: {job_id}")

    return sent

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
    """Извлекает datetime из параметров Dialogflow."""
    d = get_param(params, date_key)
    t = get_param(params, time_key)
    if d:
        return parse_date_time(d, t)
    return None

# ===================== PARSER FROM TEXT =====================
def parse_event_from_text(text):
    """Парсит название, дату и время из текста сообщения."""
    cleaned = re.sub(r'^(создать|сделать|создай|сделай|напоминание|событие|event|reminder|добавить|новое|создать событие|создать напоминание)\s+', '', text, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+(создать|сделать|создай|сделай|напоминание|событие|event|reminder)\s+', ' ', cleaned, flags=re.IGNORECASE)

    # Ищем дату и время в тексте
    patterns = [
        (r'(\d{1,2}\.\d{1,2}\.\d{4})\s+(\d{1,2}:\d{2})', True),  # дд.мм.гггг чч:мм
        (r'(\d{1,2}\.\d{1,2}\.\d{2})\s+(\d{1,2}:\d{2})', True),   # дд.мм.гг чч:мм
        (r'(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})', True),          # гггг-мм-дд чч:мм
        (r'(\d{1,2}\/\d{1,2}\/\d{4})\s+(\d{1,2}:\d{2})', True),     # мм/дд/гггг чч:мм
        (r'(\d{1,2}\.\d{1,2}\.\d{4})', False),                       # дд.мм.гггг
        (r'(\d{1,2}\.\d{1,2}\.\d{2})', False),                        # дд.мм.гг
        (r'(\d{4}-\d{2}-\d{2})', False),                               # гггг-мм-дд
    ]

    start_dt = None
    title = cleaned.strip()

    for pattern, has_time in patterns:
        match = re.search(pattern, cleaned)
        if match:
            if has_time:
                start_dt = parse_date_time(match.group(1), match.group(2))
            else:
                start_dt = parse_date_time(match.group(1), None)

            if start_dt:
                # Название — всё до даты
                title = cleaned[:match.start()].strip()
                # Убираем лишние слова из начала
                title = re.sub(r'^(создать|сделать|создай|сделай|напоминание|событие|event|reminder|добавить|новое)\s+', '', title, flags=re.IGNORECASE)
                break

    return title, start_dt

def parse_olympiad_from_text(text):
    """Парсит олимпиаду из текста: название, дата_начала, дата_конца, предмет, уровень."""
    cleaned = re.sub(r'^олимпиада\s+', '', text, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+олимпиада\s+', ' ', cleaned, flags=re.IGNORECASE)

    # Ищем даты
    date_pattern = r'(\d{1,2}\.\d{1,2}\.\d{4})'
    dates = re.findall(date_pattern, cleaned)

    start_dt = parse_date_time(dates[0], None) if len(dates) > 0 else None
    end_dt = parse_date_time(dates[1], None) if len(dates) > 1 else None

    # Убираем даты из текста
    temp_text = re.sub(date_pattern, '', cleaned, count=2)
    parts = [p.strip() for p in temp_text.split() if p.strip()]

    # Ищем уровень (1, 2, 3) в конце
    level = None
    if parts and parts[-1] in ['1', '2', '3']:
        level = parts[-1]
        parts = parts[:-1]

    # Предмет — последнее слово (или несколько)
    subject = ""
    if parts:
        # Пробуем распознать предмет
        subjects = ["математика", "физика", "химия", "биология", "информатика", 
                    "русский", "английский", "история", "обществознание", "литература",
                    "география", "астрономия", "экология", "технология"]
        for i in range(len(parts)):
            for subj in subjects:
                if subj in parts[i].lower():
                    subject = parts[i]
                    parts = parts[:i] + parts[i+1:]
                    break
            if subject:
                break

        if not subject and parts:
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

    # Логируем кратко (первые 800 символов)
    req_str = json.dumps(req, ensure_ascii=False)
    logger.info(f"=== WEBHOOK === {req_str[:800]}")

    intent_name = req.get("queryResult", {}).get("intent", {}).get("displayName", "").lower()
    params = req.get("queryResult", {}).get("parameters", {})
    query_text = req.get("queryResult", {}).get("queryText", "")
    all_required_present = req.get("queryResult", {}).get("allRequiredParamsPresent", False)
    session = req.get("session", "")

    # Получаем chat_id
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

    # Проверяем просроченные напоминания (на случай, если сервер был выключен)
    overdue_sent = check_and_send_overdue_reminders(chat_id)
    if overdue_sent > 0:
        logger.info(f"Sent {overdue_sent} overdue reminders")

    # --- CHAT ID SETUP ---
    if "chat_id" in intent_name or "chatid" in intent_name or re.search(r'chat[-_]?id', query_text, re.IGNORECASE):
        # Пользователь вводит chat_id
        match = re.search(r'(\d{7,})', query_text)
        if match:
            new_chat_id = int(match.group(1))
            session_chat_ids[session] = new_chat_id
            save_chat_id_data()
            send_telegram_message(new_chat_id, "✅ Chat_id сохранён! Теперь вы можете создавать события.")
            return jsonify({"fulfillmentText": "Chat_id сохранён!"})
        else:
            return ask_for_chat_id()

    # --- GREETING / START ---
    if any(word in intent_name for word in ["privet", "start", "hello", "greeting", "welcome", "poka"]):
        return handle_greeting(chat_id)

    # --- REMINDER / EVENT ---
    if any(word in intent_name for word in ["napomin", "reminder", "sobytie", "event", "sozdat", "dobavit"]):
        return handle_reminder(params, query_text, chat_id, all_required_present)

    # --- OLYMPIAD ---
    elif any(word in intent_name for word in ["olimpiad", "olympiad"]):
        return handle_olympiad(params, query_text, chat_id, all_required_present)

    # --- CANCEL / DELETE ---
    elif any(word in intent_name for word in ["otmen", "cancel", "delete", "udal", "ubrat"]):
        return handle_cancel(params, query_text, chat_id)

    # --- LIST / SHOW ---
    elif any(word in intent_name for word in ["spisok", "list", "pokaz", "show", "vse", "vse sobytiya"]):
        return handle_list(chat_id)

    # --- UNKNOWN ---
    logger.info(f"Unknown intent: {intent_name}")
    return jsonify({"fulfillmentText": "Не понял команду. Используйте кнопки меню или напишите «помощь»."})

# ===================== HANDLERS =====================

def handle_greeting(chat_id):
    """Приветствие и показ кнопок."""
    welcome_text = (
        "👋 Привет! Я бот для напоминаний!\n\n"
        "📌 <b>Что я умею:</b>\n"
        "• Создавать события с напоминаниями (за 7 дней, 2 дня, 1 день и в момент события)\n"
        "• Добавлять олимпиады с отслеживанием\n"
        "• Удалять созданные события\n"
        "• Показывать список всех событий\n\n"
        "📝 <b>Как создать событие:</b>\n"
        "Напишите: «создать событие Название дд.мм.гггг чч:мм»\n\n"
        "🏆 <b>Как добавить олимпиаду:</b>\n"
        "Напишите: «олимпиада Название дд.мм.гггг дд.мм.гггг Предмет Уровень»\n\n"
        "🗑 <b>Как удалить:</b>\n"
        "Напишите: «удалить Название дд.мм.гггг»\n\n"
        "📋 <b>Список событий:</b>\n"
        "Напишите: «список» или «все события»"
    )
    send_telegram_message(chat_id, welcome_text, get_main_keyboard())
    return jsonify({"fulfillmentText": "Привет! Я показал меню с кнопками."})

def handle_reminder(params, query_text, chat_id, all_required_present):
    """Обрабатывает создание события."""
    # Если Dialogflow ещё собирает параметры — не создаём событие
    if not all_required_present:
        logger.info("Slot filling in progress, not creating event yet")
        return jsonify({"fulfillmentText": ""})

    # Пробуем получить из параметров Dialogflow
    name = get_param(params, "name")
    start_dt = extract_datetime(params, "date", "time")

    # Fallback: парсим из текста
    if not name or not start_dt:
        parsed_name, parsed_dt = parse_event_from_text(query_text)
        if parsed_name and not name:
            name = parsed_name
        if parsed_dt and not start_dt:
            start_dt = parsed_dt

    # Если всё ещё не хватает — ошибка
    if not name or not start_dt:
        logger.warning(f"Missing params: name={name}, start_dt={start_dt}")
        return jsonify({"fulfillmentText": "Не удалось распознать данные. Попробуйте: создать событие Название дд.мм.гггг чч:мм"})

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

    event_data = {
        "chat_id": chat_id,
        "event_type": "event",
        "name": name,
        "start_dt": start_dt,
        "end_dt": end_dt,
        "subject": None,
        "level": None,
        "job_ids": job_ids,
    }
    events.append(event_data)
    save_data()

    response_text = f"Добавлено новое событие «{name}» с {format_dt(start_dt)} по {format_dt(end_dt)}"
    logger.info(f"Event created: {response_text}")

    send_telegram_message(chat_id, f"✅ {response_text}")

    return jsonify({"fulfillmentText": response_text})

def handle_olympiad(params, query_text, chat_id, all_required_present):
    """Обрабатывает создание олимпиады."""
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

    event_data = {
        "chat_id": chat_id,
        "event_type": "olympiad",
        "name": name,
        "start_dt": start_dt,
        "end_dt": end_dt,
        "subject": subject,
        "level": level,
        "job_ids": job_ids,
    }
    events.append(event_data)
    save_data()

    response_text = f"Добавлена новая олимпиада «{name}» {subject} {level} уровня с {format_dt(start_dt)} по {format_dt(end_dt)}"
    logger.info(f"Olympiad created: {response_text}")

    send_telegram_message(chat_id, f"✅ {response_text}")

    return jsonify({"fulfillmentText": response_text})

def handle_cancel(params, query_text, chat_id):
    """Удаление события или олимпиады."""
    name = get_param(params, "any")
    date_val = get_param(params, "date")

    start_dt = parse_date_time(date_val, None) if date_val else None

    # Fallback: парсим из текста
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
            return jsonify({"fulfillmentText": response_text})

    response_text = "Не нашёл событие"
    logger.info(f"Cancel failed: {response_text} for name={name}")
    send_telegram_message(chat_id, f"❌ {response_text}")
    return jsonify({"fulfillmentText": response_text})

def handle_list(chat_id):
    """Показывает список всех событий пользователя."""
    user_events = [e for e in events if e["chat_id"] == chat_id]

    if not user_events:
        send_telegram_message(chat_id, "📭 У вас пока нет событий.")
        return jsonify({"fulfillmentText": "У вас пока нет событий."})

    # Сортируем по дате начала
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
    return jsonify({"fulfillmentText": f"Всего событий: {len(user_events)}"})

# ===================== HEALTH CHECK =====================
@app.route('/health', methods=['GET'])
def health():
    now_local = datetime.now(USER_TIMEZONE)
    now_utc = datetime.now(UTC)
    return jsonify({
        "status": "ok",
        "events_count": len(events),
        "scheduled_jobs": len(scheduled_jobs),
        "chat_ids_saved": len(session_chat_ids),
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
