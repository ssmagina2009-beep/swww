import os
import json
import re
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
import requests

app = Flask(__name__)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "NOT_SET")

# Проверка токена
print(f"=== BOT_TOKEN loaded: {'YES' if BOT_TOKEN != 'NOT_SET' else 'NO'} ===")

scheduler = BackgroundScheduler()
scheduler.start()

events_db = []

def send_msg(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
        print(f"Telegram response: {r.status_code} - {r.text[:100]}")
    except Exception as e:
        print(f"Send error: {e}")

@app.route('/webhook', methods=['POST'])
def webhook():
    req = request.get_json(silent=True, force=True)
    print(f"=== WEBHOOK RECEIVED ===")
    print(json.dumps(req, ensure_ascii=False, indent=2)[:500])
    
    intent = req.get("queryResult", {}).get("intent", {}).get("displayName", "").lower()
    params = req.get("queryResult", {}).get("parameters", {})
    query = req.get("queryResult", {}).get("queryText", "")
    
    # Получаем chat_id
    chat_id = None
    contexts = req.get("queryResult", {}).get("outputContexts", [])
    for ctx in contexts:
        cp = ctx.get("parameters", {})
        chat_id = cp.get("chat_id") or cp.get("chatId") or cp.get("from_id")
        if chat_id: 
            break
    
    if not chat_id:
        orig = req.get("originalDetectIntentRequest", {}).get("payload", {}).get("data", {})
        if "message" in orig:
            chat_id = orig["message"]["chat"]["id"]
    
    print(f"Intent: {intent}, ChatID: {chat_id}, Query: {query}")
    
    if not chat_id:
        return jsonify({"fulfillmentText": "Ошибка: не найден chat_id"})
    
    # ПРИВЕТ
    if "privet" in intent or "start" in intent or "hello" in intent:
        send_msg(chat_id, "👋 Бот работает! Chat ID: " + str(chat_id))
        return jsonify({"fulfillmentText": "Привет! Бот ответил в Telegram."})
    
    # СОБЫТИЕ
    if "napomin" in intent or "reminder" in intent or "sobytie" in intent:
        name = params.get("name") or "Без названия"
        date = params.get("date")
        time = params.get("time")
        print(f"Event params: name={name}, date={date}, time={time}")
        
        # Планируем напоминание на 1 минуту (для теста)
        test_time = datetime.now() + timedelta(minutes=1)
        scheduler.add_job(
            send_msg,
            trigger=DateTrigger(run_date=test_time),
            args=[chat_id, f"⏰ ТЕСТОВОЕ НАПОМИНАНИЕ: {name}"],
            id=f"test_{chat_id}_{name}",
            replace_existing=True
        )
        
        events_db.append({"name": name, "chat_id": chat_id})
        msg = f'✅ Событие «{name}» создано! Напоминание через 1 минуту (тест).'
        send_msg(chat_id, msg)
        return jsonify({"fulfillmentText": msg})
    
    # ОТМЕНА
    if "otmen" in intent or "cancel" in intent or "udal" in intent:
        name = params.get("any") or params.get("name")
        print(f"Cancel search: {name}, events: {len(events_db)}")
        
        for i, ev in enumerate(events_db):
            if name and name.lower() in ev["name"].lower():
                del events_db[i]
                msg = f'🗑 Удалил «{ev["name"]}»'
                send_msg(chat_id, msg)
                return jsonify({"fulfillmentText": msg})
        
        msg = "❌ Не нашел событие"
        send_msg(chat_id, msg)
        return jsonify({"fulfillmentText": msg})
    
    # ОЛИМПИАДА
    if "olimpiad" in intent:
        name = params.get("any") or "Олимпиада"
        subject = params.get("subject") or ""
        level = params.get("level") or ""
        msg = f'✅ Олимпиада «{name}» {subject} {level} уровня добавлена!'
        send_msg(chat_id, msg)
        return jsonify({"fulfillmentText": msg})
    
    return jsonify({"fulfillmentText": f"Получил: {query}"})

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "events": len(events_db)})

@app.route('/')
def index():
    return "Bot is running!"

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
