from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

TOKEN = "YOUR_BOT_TOKEN"

scheduler = AsyncIOScheduler()
scheduler.start()


def parse_message(text):
    """
    Формат сообщения:

    Название события
    2026-08-15 14:00
    2026-08-15 18:00
    """

    lines = text.strip().split("\n")

    if len(lines) != 3:
        return None

    title = lines[0]

    try:
        start = datetime.strptime(lines[1], "%Y-%m-%d %H:%M")
        end = datetime.strptime(lines[2], "%Y-%m-%d %H:%M")
    except:
        return None

    return title, start, end


async def send_reminder(chat_id, bot, title, start, end, hours):

    if hours == 0:
        text = (
            f"Напоминание! Сейчас начинается событие "
            f"«{title}» с {start.strftime('%d.%m.%Y %H:%M')} "
            f"по {end.strftime('%d.%m.%Y %H:%M')}."
        )
    else:
        text = (
            f"Напоминание! Через {hours} часов будет событие "
            f"«{title}» с {start.strftime('%d.%m.%Y %H:%M')} "
            f"по {end.strftime('%d.%m.%Y %H:%M')}."
        )

    await bot.send_message(chat_id=chat_id, text=text)


async def add_event(update: Update, context: ContextTypes.DEFAULT_TYPE):

    data = parse_message(update.message.text)

    if data is None:
        await update.message.reply_text(
            "Введите данные в формате:\n\n"
            "Название события\n"
            "2026-08-15 14:00\n"
            "2026-08-15 18:00"
        )
        return

    title, start, end = data

    await update.message.reply_text(
        f"Добавлено новое событие «{title}» "
        f"с {start.strftime('%d.%m.%Y %H:%M')} "
        f"по {end.strftime('%d.%m.%Y %H:%M')}."
    )

    reminders = [
        168,
        48,
        24,
        0,
    ]

    for hours in reminders:

        remind_time = start - timedelta(hours=hours)

        if remind_time > datetime.now():

            scheduler.add_job(
                send_reminder,
                "date",
                run_date=remind_time,
                args=[
                    update.effective_chat.id,
                    context.bot,
                    title,
                    start,
                    end,
                    hours,
                ],
            )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "Отправьте сообщение в формате:\n\n"
        "Название события\n"
        "2026-08-15 14:00\n"
        "2026-08-15 18:00"
    )


def main():

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, add_event)
    )

    app.run_polling()


if __name__ == "__main__":
    main()
