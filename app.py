import sqlite3
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    filters,
)

from apscheduler.schedulers.asyncio import AsyncIOScheduler


TOKEN = "YOUR_BOT_TOKEN"

DB = "events.db"

scheduler = AsyncIOScheduler()


# ---------- БАЗА ДАННЫХ ----------

def init_db():
    conn = sqlite3.connect(DB)
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        type TEXT,
        title TEXT,
        subject TEXT,
        level TEXT,
        start TEXT,
        end TEXT
    )
    """)

    conn.commit()
    conn.close()


def save_event(data):
    conn = sqlite3.connect(DB)
    cursor = conn.cursor()

    cursor.execute("""
    INSERT INTO events
    (chat_id, type, title, subject, level, start, end)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, data)

    conn.commit()
    conn.close()


def delete_event(chat_id, title, start):
    conn = sqlite3.connect(DB)
    cursor = conn.cursor()

    cursor.execute("""
    DELETE FROM events
    WHERE chat_id=?
    AND title=?
    AND start=?
    """,
    (
        chat_id,
        title,
        start
    ))

    result = cursor.rowcount

    conn.commit()
    conn.close()

    return result > 0


# ---------- НАПОМИНАНИЯ ----------

async def send_reminder(
        bot,
        chat_id,
        event_type,
        title,
        subject,
        level,
        start,
        end,
        hours
):

    if hours == 0:
        prefix = "Сейчас начинается"
    else:
        prefix = f"Через {hours} часов будет"

    if event_type == "напоминание":

        text = (
            f"{prefix} событие "
            f"«{title}»\n"
            f"Начало: {start}\n"
            f"Конец: {end}"
        )

    else:

        text = (
            f"{prefix} олимпиада\n"
            f"«{title}»\n"
            f"Предмет: {subject}\n"
            f"Уровень: {level}\n"
            f"Начало: {start}\n"
            f"Конец: {end}"
        )


    await bot.send_message(
        chat_id=chat_id,
        text=text
    )



def create_reminders(
        chat_id,
        event_type,
        title,
        subject,
        level,
        start,
        end,
        bot
):

    try:

        start_date = datetime.strptime(
            start,
            "%d.%m.%Y %H:%M"
        )

    except:

        return


    for hours in [168, 48, 24, 0]:

        remind_time = start_date - timedelta(hours=hours)


        if remind_time > datetime.now():

            scheduler.add_job(
                send_reminder,
                "date",
                run_date=remind_time,
                args=[
                    bot,
                    chat_id,
                    event_type,
                    title,
                    subject,
                    level,
                    start,
                    end,
                    hours
                ]
            )



# ---------- СООБЩЕНИЯ ----------


async def message_handler(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
):

    text = update.message.text.strip()

    chat_id = update.effective_chat.id

    lower = text.lower()



    # ОТМЕНА

    if (
        "отмени" in lower
        or "отмена" in lower
        or "отменить" in lower
    ):

        lines = text.split("\n")


        if len(lines) >= 3:

            title = lines[1]
            start = lines[2]


            if delete_event(
                chat_id,
                title,
                start
            ):

                await update.message.reply_text(
                    f"Удалил событие «{title}»"
                )

            else:

                await update.message.reply_text(
                    "Не нашел такое событие"
                )

        return



    # НАПОМИНАНИЕ


    if "напоминание" in lower:


        lines = text.split("\n")
        if len(lines) != 4:

            await update.message.reply_text(
                "Формат:\n\n"
                "напоминание\n"
                "Название события\n"
                "Дата и время начала\n"
                "Дата и время конца"
            )

            return


        title = lines[1]
        start = lines[2]
        end = lines[3]


        save_event(
            (
                chat_id,
                "напоминание",
                title,
                "",
                "",
                start,
                end
            )
        )


        create_reminders(
            chat_id,
            "напоминание",
            title,
            "",
            "",
            start,
            end,
            context.bot
        )


        await update.message.reply_text(
            f"Добавлено новое событие "
            f"«{title}» с {start} по {end}"
        )



    # ОЛИМПИАДА


    elif "олимпиада" in lower:


        lines = text.split("\n")


        if len(lines) != 6:

            await update.message.reply_text(
                "Формат:\n\n"
                "олимпиада\n"
                "Название олимпиады\n"
                "Предмет\n"
                "Уровень (1/2/3)\n"
                "Дата и время начала\n"
                "Дата и время конца"
            )

            return



        title = lines[1]
        subject = lines[2]
        level = lines[3]
        start = lines[4]
        end = lines[5]



        save_event(
            (
                chat_id,
                "олимпиада",
                title,
                subject,
                level,
                start,
                end
            )
        )



        create_reminders(
            chat_id,
            "олимпиада",
            title,
            subject,
            level,
            start,
            end,
            context.bot
        )



        await update.message.reply_text(
            f"Добавлена олимпиада "
            f"«{title}» {subject}, "
            f"{level} уровень\n"
            f"{start} - {end}"
        )



# ---------- ЗАПУСК ----------


async def post_init(app):

    scheduler.start()



def main():

    init_db()


    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )


    app.add_handler(
        MessageHandler(
            filters.TEXT,
            message_handler
        )
    )


    app.run_polling()



if __name__ == "__main__":
    main()
