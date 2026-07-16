
app_py_code = r'''import logging
import json
import re
import asyncio
import threading
from datetime import datetime, timedelta
from dateutil import parser as date_parser

from flask import Flask, request, jsonify
from telegram import Bot

# ===================== SETTINGS =====================
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"

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
bot = Bot(token=BOT_TOKEN)

# ===================== STORAGE =====================
events = []
scheduled_jobs = {}

# ===================== DATE/TIME PARSER =====================
def parse_date_time(date_str, time_str):
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
    if not text:
        return None
    try:
        return date_parser.parse(text.strip(), dayfirst=True)
    except Exception:
        return None

def format_dt(dt):
    return dt.strftime("%d.%m.%Y %H:%M")

# ===================== SCHEDULER =====================
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

# ===================== SEND REMINDERS =====================
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
        text = (f"Napominaie! Cherez {hours_text} chasov budet olimpiada "
                f"{data['name']} {data['subject']} {data['level']} urovnja "
                f"s {start_str} po {end_str}")
    else:
        text = (f"Napominaie! Cherez {hours_text} chasov budet sobytie "
                f"{data['name']} s {start_str} po {end_str}")
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logger.error(f"Oshibka otpravki: {e}")
    if job_id in scheduled_jobs:
        del scheduled_jobs[job_id]

# ===================== PARAM EXTRACTOR =====================
def get_param(params, name, default=None):
    val = params.get(name, default)
    if isinstance(val, dict):
        return val.get("name") or val.get("value") or str(val)
    return val

def extract_datetime(params, date_key, time_key):
    d = get_param(params, date_key)
    t = get_param(params, time_key)
    if d:
        return parse_date_time(str(d), str(t) if t else None)
    return None

# ===================== WEBHOOK =====================
@app.route('/webhook', methods=['POST'])
def webhook():
    req = request.get_json(silent=True, force=True)
    logger.info(f"Webhook received: {json.dumps(req, ensure_ascii=False)}")
    
    intent_name = req.get("queryResult", {}).get("intent", {}).get("displayName", "").lower()
    params = req.get("queryResult", {}).get("parameters", {})
    query_text = req.get("queryResult", {}).get("queryText", "")
    
    # Get chat_id from Telegram context
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
        chat_id = payload.get("data", {}).get("chat", {}).get("id")
    
    # Fallback: session
    if not chat_id:
        session = req.get("session", "")
        if "/sessions/" in session:
            chat_id = session.split("/sessions/")[-1]
    
    if not chat_id:
        logger.error("chat_id not found!")
        return jsonify({"fulfillmentText": "Oshibka: ne udalos opredelit chat."})
    
    try:
        chat_id = int(chat_id)
    except:
        pass
    
    logger.info(f"Intent: {intent_name}, chat_id: {chat_id}, params: {params}")
    
    # --- REMINDER ---
    if "napomina" in intent_name or "reminder" in intent_name:
        return handle_reminder(params, query_text, chat_id)
    
    # --- OLYMPIAD ---
    elif "olimpiad" in intent_name or "olympiad" in intent_name:
        return handle_olympiad(params, query_text, chat_id)
    
    # --- CANCEL ---
    elif "otmen" in intent_name or "cancel" in intent_name:
        return handle_cancel(params, query_text, chat_id)
    
    return jsonify({"fulfillmentText": "Ne ponjal komandu."})

def handle_reminder(params, query_text, chat_id):
    name = get_param(params, "name")
    start_dt = extract_datetime(params, "date", "time")
    
    # Parse end date from text
    end_dt = None
    end_match = re.search(r"(?:po|do)\s+([0-9]{1,2}[./-][0-9]{1,2}[./-][0-9]{2,4}(?:\s+[0-9]{1,2}:[0-9]{2})?)", query_text, re.IGNORECASE)
    if end_match:
        end_dt = parse_from_text(end_match.group(1))
    
    if not end_dt:
        all_dates = re.findall(r"[0-9]{1,2}[./-][0-9]{1,2}[./-][0-9]{2,4}(?:\s+[0-9]{1,2}:[0-9]{2})?", query_text)
        if len(all_dates) >= 2:
            end_dt = parse_from_text(all_dates[1])
    
    if not end_dt and start_dt:
        end_dt = start_dt + timedelta(hours=1)
    
    if not name or not start_dt:
        return jsonify({"fulfillmentText": "Ukažite nazvanie, datu i vremja načala sobytija."})
    
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
    
    response_text = f"Dobavleno novoe sobytie {name} s {format_dt(start_dt)} po {format_dt(end_dt)}"
    logger.info(f"Reminder response: {response_text}")
    
    return jsonify({"fulfillmentText": response_text})

def handle_olympiad(params, query_text, chat_id):
    name = get_param(params, "any")
    subject = get_param(params, "subject")
    level = get_param(params, "level")
    
    start_date = get_param(params, "FROM")
    end_date = get_param(params, "to")
    
    start_dt = parse_date_time(start_date, None) if start_date else None
    end_dt = parse_date_time(end_date, None) if end_date else None
    
    if not start_dt or not end_dt:
        all_dates = re.findall(r"[0-9]{1,2}[./-][0-9]{1,2}[./-][0-9]{2,4}", query_text)
        if len(all_dates) >= 2:
            if not start_dt:
                start_dt = parse_from_text(all_dates[0])
            if not end_dt:
                end_dt = parse_from_text(all_dates[1])
    
    if start_dt and start_dt.hour == 0 and start_dt.minute == 0:
        start_dt = start_dt.replace(hour=9, minute=0)
    if end_dt and end_dt.hour == 0 and end_dt.minute == 0:
        end_dt = end_dt.replace(hour=14, minute=0)
    
    if not name or not start_dt or not end_dt:
        return jsonify({"fulfillmentText": "Ukažite nazvanie olimpiady, predmet, uroven i daty."})
    
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
    
    response_text = f"Dobavlena novaja olimpiada {name} {subject} {level} urovnja s {format_dt(start_dt)} po {format_dt(end_dt)}"
    logger.info(f"Olympiad response: {response_text}")
    
    return jsonify({"fulfillmentText": response_text})

def handle_cancel(params, query_text, chat_id):
    name = get_param(params, "any")
    date_val = get_param(params, "date")
    
    start_dt = parse_date_time(date_val, None) if date_val else None
    
    if not start_dt:
        dt_match = re.search(r"[0-9]{1,2}[./-][0-9]{1,2}[./-][0-9]{2,4}(?:\s+[0-9]{1,2}:[0-9]{2})?", query_text)
        if dt_match:
            start_dt = parse_from_text(dt_match.group(0))
    
    if not name or not start_dt:
        return jsonify({"fulfillmentText": "Ukažite nazvanie i datu sobytija dlja otmeny."})
    
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
        return jsonify({"fulfillmentText": f"Udalil {name}"})
    else:
        return jsonify({"fulfillmentText": f"Ne nashol {name} s {format_dt(start_dt)}"})

# ===================== BACKGROUND SCHEDULER =====================
async def scheduler_loop():
    while True:
        now = datetime.now()
        for job_id, job in list(scheduled_jobs.items()):
            if job["run_at"] <= now:
                await send_reminder(job_id)
        await asyncio.sleep(30)

# ===================== RUN =====================
@app.route('/')
def index():
    return "Bot is running!"

if __name__ == "__main__":
    def run_scheduler():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(scheduler_loop())
    
    t = threading.Thread(target=run_scheduler, daemon=True)
    t.start()
    
    app.run(host="0.0.0.0", port=8080)
'''

# Заменяем транслит на русский текст
replacements = {
    'Napominaie': 'Напоминание',
    'Cherez': 'Через',
    'chasov': 'часов',
    'budet': 'будет',
    'olimpiada': 'олимпиада',
    'urovnja': 'уровня',
    's ': 'с ',
    'po ': 'по ',
    'sobytie': 'событие',
    'Oshibka': 'Ошибка',
    'otpravki': 'отправки',
    'opredelit': 'определить',
    'chat': 'чат',
    'Ne ponjal': 'Не понял',
    'komandu': 'команду',
    'Ukažite': 'Укажите',
    'nazvanie': 'название',
    'datu': 'дату',
    'vremja': 'время',
    'načala': 'начала',
    'sobytija': 'события',
    'Dobavleno': 'Добавлено',
    'novoe': 'новое',
    'sobytie': 'событие',
    'predmet': 'предмет',
    'uroven': 'уровень',
    'Dobavlena': 'Добавлена',
    'novaja': 'новая',
    'olimpiada': 'олимпиада',
    'dlja': 'для',
    'otmeny': 'отмены',
    'Udalil': 'Удалил',
    'Ne nashol': 'Не нашёл',
    'Webhook received': 'Получен webhook',
    'Reminder response': 'Ответ напоминания',
    'Olympiad response': 'Ответ олимпиады',
    'chat_id not found': 'chat_id не найден',
    'Intent': 'Интент',
}

# Делаем замены аккуратно
for old, new in replacements.items():
    app_py_code = app_py_code.replace(old, new)

with open('/mnt/agents/output/app.py', 'w', encoding='utf-8') as f:
    f.write(app_py_code)

print("app.py updated with Russian text!")
print(f"Size: {len(app_py_code)} chars")

