import logging
import json
import re
import asyncio
import threading
from datetime import datetime, timedelta
from dateutil import parser as date_parser

from flask import Flask, request, jsonify
from telegram import Bot

# ===================== НАСТРОЙКИ =====================
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"  # Токен Telegram бота от @BotFather

# Время напоминаний (часов до события)
REMINDER_DELTAS = [168, 48, 24, 0]

# ===================== ЛОГИРОВАНИЕ =====================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ===================== ИНИЦИАЛИЗАЦИЯ =====================
app = Flask(__name__)
bot = Bot(token=BOT_TOKEN)

# ===================== ХРАНИЛИЩЕ =====================
events = []
scheduled_jobs = {}

# ===================== ПАРСЕР ДАТ/ВРЕМЕНИ =====================
def parse_date_time(date_str, time_str):
    """Собирает datetime из date и time от Dialogflow."""
    if not date_str:
        return None

    date_str = str(date_str).strip()
    time_str = str(time_str).strip() if time_str else ""

    try:
        if time_str:
            dt_str = f"{date_str} {time_str}"
            return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        else:
            return datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        try:
            if time_str:
                dt_str = f"{date_str} {time_str}"
                return datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
            else:
                return datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            try:
                full = f"{date_str} {time_str}" if time_str else date_str
                return date_parser.parse(full)
            except Exception:
                return None

def parse_from_text(text):
    """Парсит дату-время из произвольного текста."""
    if not text:
        return None
    try:
        return date_parser.parse(text.strip(), dayfirst=True)
    except Exception:
        return None

def format_dt(dt):
    return dt.strftime("%d.%m.%Y %H:%M")

# ===================== ПЛАНИРОВЩИК =====================
def schedule_reminders(chat_id, event_type, name, start_dt, end_dt, subject=None, level=None):
    job_ids = []
    now = datetime.now()

    for delta_hours in REMINDER_DELTAS:
        reminder_time = start_dt - timedelta(hours=delta_hours)
        if reminder_time <= now:
            continue

        job_id = f"{chat_id}_{name}_{start_dt.isoformat()}_{delta_hours}"
        scheduled_jobs[job_id] = {
            "chat_id": chat_id,
            "run_at": reminder_time,
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

# ===================== ОТПРАВКА НАПОМИНАНИЙ =====================
async def send_reminder(job_id):
    if job_id not in scheduled_jobs:
        return

    job = scheduled_jobs[job_id]
    data = job["data"]
    chat_id = job["chat_id"]

    hours_text = str(data["delta_hours"]) if data["delta_hours"] > 0 else "0"
    start_str = format_dt(data["start_dt"])
    end_str = format_dt(data["end_dt"])

    if data["event_type"] == "olympiad":
        text = (f"Напоминание! Через «{hours_text}» часов будет олимпиада "
                f"«{data['name']}» {data['subject']} {data['level']} уровня "
                f"с {start_str} по {end_str}❤️")
    else:
        text = (f"Напоминание! Через «{hours_text}» часов будет событие "
                f"«{data['name']}» с {start_str} по {end_str}❤️")

    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")

    if job_id in scheduled_jobs:
        del scheduled_jobs[job_id]

# ===================== ИЗВЛЕЧЕНИЕ ПАРАМЕТРОВ =====================
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
        return parse_date_time(str(d), str(t) if t else None)
    return None

# ===================== WEBHOOK =====================
@app.route('/webhook', methods=['POST'])
def webhook():
    req = request.get_json(silent=True, force=True)

    intent_name = req.get("queryResult", {}).get("intent", {}).get("displayName", "").lower()
    params = req.get("queryResult", {}).get("parameters", {})
    query_text = req.get("queryResult", {}).get("queryText", "")

    # Получаем chat_id
    output_contexts = req.get("queryResult", {}).get("outputContexts", [])
    chat_id = None

    for ctx in output_contexts:
        ctx_params = ctx.get("parameters", {})
        chat_id = ctx_params.get("chat_id") or ctx_params.get("from_id")
        if chat_id:
            break

    if not chat_id:
        session = req.get("session", "")
        if "/sessions/" in session:
            chat_id = session.split("/sessions/")[-1]

    if not chat_id:
        return jsonify({"fulfillmentText": "❌ Не удалось определить чат."})

    try:
        chat_id = int(chat_id)
    except:
        pass

    # --- НАПОМИНАНИЕ ---
    if "напоминание" in intent_name or "reminder" in intent_name:
        return handle_reminder(params, query_text, chat_id)

    # --- ОЛИМПИАДА ---
    elif "олимпиад" in intent_name or "olympiad" in intent_name:
        return handle_olympiad(params, query_text, chat_id)

    # --- ОТМЕНА ---
    elif "отмен" in intent_name or "cancel" in intent_name:
        return handle_cancel(params, query_text, chat_id)

    return jsonify({"fulfillmentText": "🤔 Не понял команду."})

def handle_reminder(params, query_text, chat_id):
    """
    Параметры Dialogflow:
    - name: @sys.any (название события)
    - date: @sys.date (дата начала)
    - time: @sys.time (время начала)

    Дата окончания НЕТ в параметрах — парсим из текста.
    """
    name = get_param(params, "name")
    start_dt = extract_datetime(params, "date", "time")

    # Парсим дату окончания из текста запроса
    end_dt = None
    # Ищем "по ДД.ММ.ГГГГ ЧЧ:ММ" или "до ДД.ММ.ГГГГ ЧЧ:ММ"
    end_match = re.search(r"(?:по|до)\s+([0-9]{1,2}[./-][0-9]{1,2}[./-][0-9]{2,4}(?:\s+[0-9]{1,2}:[0-9]{2})?)", query_text, re.IGNORECASE)
    if end_match:
        end_dt = parse_from_text(end_match.group(1))

    # Если не нашли "по/до", ищем вторую дату в тексте
    if not end_dt:
        all_dates = re.findall(r"[0-9]{1,2}[./-][0-9]{1,2}[./-][0-9]{2,4}(?:\s+[0-9]{1,2}:[0-9]{2})?", query_text)
        if len(all_dates) >= 2:
            end_dt = parse_from_text(all_dates[1])

    # Fallback: end = start + 1 час
    if not end_dt and start_dt:
        end_dt = start_dt + timedelta(hours=1)

    if not name or not start_dt:
        return jsonify({"fulfillmentText": "❌ Укажите название, дату и время начала события."})

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

    return jsonify({
        "fulfillmentText": f"Добавлено новое событие «{name}» с {format_dt(start_dt)} по {format_dt(end_dt)}"
    })

def handle_olympiad(params, query_text, chat_id):
    """
    Параметры Dialogflow:
    - any: @sys.any (название олимпиады)
    - subject: @subje (предмет)
    - level: @level (уровень 1/2/3)
    - FROM: @sys.d (дата начала)
    - to: @sys.d (дата окончания)

    ВРЕМЕНИ НЕТ — ставим по умолчанию 09:00 и 14:00.
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
        return jsonify({"fulfillmentText": "❌ Укажите название олимпиады, предмет, уровень и даты."})

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

    return jsonify({
        "fulfillmentText": f"Добавлена новая олимпиада «{name}» {subject} {level} уровня с {format_dt(start_dt)} по {format_dt(end_dt)}"
    })

def handle_cancel(params, query_text, chat_id):
    """
    Параметры Dialogflow:
    - any: @sys.a (название события/олимпиады)
    - date: @sys.d (дата начала)

    ВРЕМЕНИ НЕТ — ищем в тексте или используем 00:00.
    """
    name = get_param(params, "any")
    date_val = get_param(params, "date")

    start_dt = parse_date_time(date_val, None) if date_val else None

    # Fallback: парсим дату-время из текста
    if not start_dt:
        dt_match = re.search(r"[0-9]{1,2}[./-][0-9]{1,2}[./-][0-9]{2,4}(?:\s+[0-9]{1,2}:[0-9]{2})?", query_text)
        if dt_match:
            start_dt = parse_from_text(dt_match.group(0))

    if not name or not start_dt:
        return jsonify({"fulfillmentText": "❌ Укажите название и дату события для отмены."})

    found = False
    to_remove = []

    for i, event in enumerate(events):
        if (
            event["chat_id"] == chat_id
            and event["name"].lower() == name.lower()
            and event["start_dt"].strftime("%d.%m.%Y %H:%M") == start_dt.strftime("%d.%m.%Y %H:%M")
        ):
            for job_id in event["job_ids"]:
                if job_id in scheduled_jobs:
                    del scheduled_jobs[job_id]
            to_remove.append(i)
            found = True

    for idx in reversed(to_remove):
        events.pop(idx)

    if found:
        return jsonify({"fulfillmentText": f"Удалил «{name}»❤️"})
    else:
        return jsonify({"fulfillmentText": f"Не нашёл «{name}» с {format_dt(start_dt)}"})

# ===================== ФОНОВЫЙ ПЛАНИРОВЩИК =====================
async def scheduler_loop():
    while True:
        now = datetime.now()
        for job_id, job in list(scheduled_jobs.items()):
            if job["run_at"] <= now:
                await send_reminder(job_id)
        await asyncio.sleep(30)

# ===================== ЗАПУСК =====================
@app.route('/')
def index():
    return "Bot is running!"

if __name__ == "__main__":
    # Запускаем планировщик
    def run_scheduler():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(scheduler_loop())

    t = threading.Thread(target=run_scheduler, daemon=True)
    t.start()

    app.run(host="0.0.0.0", port=8080)
