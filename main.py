import logging
import os
import sqlite3

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

WAITING_COMMENT = 1


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS users "
        "(user_id INTEGER PRIMARY KEY, blocked INTEGER DEFAULT 0)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS works "
        "(work_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "author_id INTEGER, file_id TEXT, caption TEXT, msg_type TEXT)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS ratings "
        "(work_id INTEGER, voter_id INTEGER, score INTEGER, "
        "PRIMARY KEY(work_id, voter_id))"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS contacts (work_id INTEGER, voter_id INTEGER)"
    )
    conn.commit()
    conn.close()


def is_blocked(user_id: int) -> bool:
    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()
    cur.execute("SELECT blocked FROM users WHERE user_id=?", (user_id,))
    res = cur.fetchone()
    conn.close()
    return bool(res and res[0] == 1)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()
    await update.message.reply_text(
        "Привет! Отправь свою работу (фото/видео/документ/текст) — "
        "она анонимно уйдёт всем зарегистрированным пользователям.\n"
        "Они смогут поставить оценку 1–5, оставить комментарий, "
        "связаться с тобой или пожаловаться на работу."
    )


async def receive_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if is_blocked(user_id):
        await update.message.reply_text("Вы заблокированы и не можете отправлять работы.")
        return

    file_id = None
    caption = None
    msg_type = None

    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        caption = update.message.caption
        msg_type = "photo"
    elif update.message.video:
        file_id = update.message.video.file_id
        caption = update.message.caption
        msg_type = "video"
    elif update.message.document:
        file_id = update.message.document.file_id
        caption = update.message.caption
        msg_type = "document"
    elif update.message.text:
        caption = update.message.text
        msg_type = "text"
    else:
        await update.message.reply_text(
            "Поддерживаются фото, видео, документы и текст."
        )
        return

    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO works (author_id, file_id, caption, msg_type) VALUES (?,?,?,?)",
        (user_id, file_id, caption, msg_type),
    )
    work_id = cur.lastrowid
    conn.commit()

    keyboard = [
        [InlineKeyboardButton(str(i), callback_data=f"rate_{work_id}_{i}") for i in range(1, 6)],
        [
            InlineKeyboardButton("💬 Комментарий", callback_data=f"comment_{work_id}"),
            InlineKeyboardButton("🤝 Связаться", callback_data=f"contact_{work_id}"),
        ],
        [InlineKeyboardButton("⚠️ Пожаловаться", callback_data=f"complain_{work_id}")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    cur.execute(
        "SELECT user_id FROM users WHERE user_id != ? AND blocked = 0", (user_id,)
    )
    recipients = cur.fetchall()
    conn.close()

    for (uid,) in recipients:
        try:
            if msg_type in ("photo", "video", "document"):
                await context.bot.copy_message(
                    chat_id=uid,
                    from_chat_id=update.message.chat_id,
                    message_id=update.message.message_id,
                    reply_markup=reply_markup,
                )
            else:
                await context.bot.send_message(
                    chat_id=uid, text=caption, reply_markup=reply_markup
                )
        except Exception as e:
            logger.warning("Ошибка отправки пользователю %s: %s", uid, e)

    await update.message.reply_text("✅ Работа отправлена на оценку!")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    action = parts[0]
    work_id = int(parts[1])

    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()

    if action == "rate":
        score = int(parts[2])
        voter = query.from_user.id
        cur.execute(
            "SELECT * FROM ratings WHERE work_id=? AND voter_id=?", (work_id, voter)
        )
        if cur.fetchone():
            await query.edit_message_text("Вы уже оценили эту работу.")
            conn.close()
            return
        cur.execute(
            "INSERT INTO ratings (work_id, voter_id, score) VALUES (?,?,?)",
            (work_id, voter, score),
        )
        conn.commit()
        cur.execute("SELECT author_id FROM works WHERE work_id=?", (work_id,))
        row = cur.fetchone()
        if row:
            author_id = row[0]
            cur.execute("SELECT AVG(score) FROM ratings WHERE work_id=?", (work_id,))
            avg = cur.fetchone()[0]
            await context.bot.send_message(
                chat_id=author_id,
                text=f"📊 Новая оценка вашей работы: {score}\nСредний балл: {avg:.1f}",
            )
        await query.edit_message_text(f"Вы поставили оценку {score} ✅")

    elif action == "comment":
        await query.edit_message_text(
            "📝 Напишите свой комментарий одним сообщением."
        )
        context.user_data["comment_work_id"] = work_id
        conn.close()
        return WAITING_COMMENT

    elif action == "contact":
        voter = query.from_user.id
        cur.execute("SELECT author_id FROM works WHERE work_id=?", (work_id,))
        row = cur.fetchone()
        if row:
            author_id = row[0]
            username = query.from_user.username
            uname_str = f"@{username}" if username else f"ID: {voter}"
            await context.bot.send_message(
                chat_id=author_id,
                text=f"🤝 Пользователь {uname_str} хочет с вами связаться.\n"
                     f"Его ID: {voter}",
            )
        await query.edit_message_text("✅ Ваш контакт отправлен автору.")

    elif action == "complain":
        cur.execute(
            "SELECT author_id, file_id, caption FROM works WHERE work_id=?", (work_id,)
        )
        row = cur.fetchone()
        if row:
            author_id, file_id, caption = row
            admin_msg = f"⚠️ Жалоба на работу #{work_id}\nАвтор ID: {author_id}"
            if caption:
                admin_msg += f"\nТекст: {caption}"
            await context.bot.send_message(chat_id=ADMIN_ID, text=admin_msg)
            if file_id:
                await context.bot.copy_message(
                    chat_id=ADMIN_ID,
                    from_chat_id=query.message.chat_id,
                    message_id=query.message.message_id,
                )
        await query.edit_message_text("✅ Жалоба отправлена администратору.")

    conn.close()


async def receive_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    work_id = context.user_data.get("comment_work_id")
    if not work_id:
        await update.message.reply_text(
            "Сначала нажмите кнопку «Комментарий» под работой."
        )
        return ConversationHandler.END

    comment = update.message.text
    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()
    cur.execute("SELECT author_id FROM works WHERE work_id=?", (work_id,))
    row = cur.fetchone()
    conn.close()

    if row:
        author_id = row[0]
        await context.bot.send_message(
            chat_id=author_id,
            text=f"💬 Комментарий к вашей работе #{work_id}:\n{comment}",
        )
    await update.message.reply_text("✅ Комментарий отправлен автору.")
    return ConversationHandler.END


async def block_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Нет доступа.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /block <user_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Укажите числовой ID пользователя.")
        return
    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()
    cur.execute("UPDATE users SET blocked = 1 WHERE user_id=?", (target_id,))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Пользователь {target_id} заблокирован.")


async def unblock_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Нет доступа.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /unblock <user_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Укажите числовой ID пользователя.")
        return
    conn = sqlite3.connect("bot_data.db")
    cur = conn.cursor()
    cur.execute("UPDATE users SET blocked = 0 WHERE user_id=?", (target_id,))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Пользователь {target_id} разблокирован.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if not TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set. Add it to environment secrets."
        )
    if not ADMIN_ID:
        raise RuntimeError(
            "ADMIN_ID is not set. Add it to environment variables."
        )

    init_db()

    app = Application.builder().token(TOKEN).build()

    # Conversation handler for the comment flow
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern=r"^comment_")],
        states={
            WAITING_COMMENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_comment)
            ]
        },
        fallbacks=[],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("block", block_user))
    app.add_handler(CommandHandler("unblock", unblock_user))
    app.add_handler(conv_handler)
    app.add_handler(
        CallbackQueryHandler(button_handler, pattern=r"^(rate|contact|complain)_")
    )
    app.add_handler(
        MessageHandler(
            (filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.TEXT)
            & ~filters.COMMAND,
            receive_work,
        )
    )

    logger.info("Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
