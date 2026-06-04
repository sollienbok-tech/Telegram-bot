import logging
import os
import sqlite3

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

WAITING_COMMENT = 1

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Database ────────────────────────────────────────────────────────────────

def get_conn():
    return sqlite3.connect("bot_data.db")


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS users "
        "(user_id INTEGER PRIMARY KEY, username TEXT, blocked INTEGER DEFAULT 0)"
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
        "CREATE TABLE IF NOT EXISTS contacts "
        "(work_id INTEGER, voter_id INTEGER, PRIMARY KEY(work_id, voter_id))"
    )
    # migrate: add msg_type column if it doesn't exist yet
    try:
        cur.execute("ALTER TABLE works ADD COLUMN msg_type TEXT")
    except Exception:
        pass
    conn.commit()
    conn.close()


def is_blocked(user_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT blocked FROM users WHERE user_id=?", (user_id,))
    res = cur.fetchone()
    conn.close()
    return bool(res and res[0] == 1)


def upsert_user(user_id: int, username: str | None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (user_id, username) VALUES (?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
        (user_id, username or ""),
    )
    conn.commit()
    conn.close()


# ─── Helpers ─────────────────────────────────────────────────────────────────

def build_work_keyboard(work_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⭐ 1", callback_data=f"rate_{work_id}_1"),
            InlineKeyboardButton("⭐ 2", callback_data=f"rate_{work_id}_2"),
            InlineKeyboardButton("⭐ 3", callback_data=f"rate_{work_id}_3"),
            InlineKeyboardButton("⭐ 4", callback_data=f"rate_{work_id}_4"),
            InlineKeyboardButton("⭐ 5", callback_data=f"rate_{work_id}_5"),
        ],
        [
            InlineKeyboardButton("💬 Комментарий", callback_data=f"comment_{work_id}"),
            InlineKeyboardButton("🤝 Связаться",    callback_data=f"contact_{work_id}"),
        ],
        [
            InlineKeyboardButton("⚠️ Пожаловаться", callback_data=f"complain_{work_id}"),
        ],
    ])


async def send_work_to_user(
    context: ContextTypes.DEFAULT_TYPE,
    uid: int,
    msg_type: str,
    file_id: str | None,
    caption: str | None,
    from_chat_id: int,
    message_id: int,
    reply_markup: InlineKeyboardMarkup,
):
    """Send a work to a single user using the appropriate Telegram method."""
    if msg_type in ("photo", "video", "document", "audio", "voice"):
        await context.bot.copy_message(
            chat_id=uid,
            from_chat_id=from_chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
        )
    else:
        await context.bot.send_message(
            chat_id=uid,
            text=caption or "(пустое сообщение)",
            reply_markup=reply_markup,
        )


async def forward_work_to_admin(
    context: ContextTypes.DEFAULT_TYPE,
    work_id: int,
    author_id: int,
    msg_type: str,
    file_id: str | None,
    caption: str | None,
    from_chat_id: int,
    message_id: int,
    complaint_text: str,
):
    """Forward a complained work to the admin with a block button."""
    block_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"🚫 Заблокировать {author_id}",
            callback_data=f"adminblock_{author_id}",
        )
    ]])
    await context.bot.send_message(chat_id=ADMIN_ID, text=complaint_text)
    if msg_type in ("photo", "video", "document", "audio", "voice"):
        await context.bot.copy_message(
            chat_id=ADMIN_ID,
            from_chat_id=from_chat_id,
            message_id=message_id,
            reply_markup=block_keyboard,
        )
    else:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=caption or "(без текста)",
            reply_markup=block_keyboard,
        )


# ─── Handlers ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.username)
    await update.message.reply_text(
        "🎵 Привет! Это бот для анонимной оценки творческих работ.\n\n"
        "Сюда можно отправить:\n"
        "• Кавер на песню (аудио/голосовое)\n"
        "• Концепт для группы (текст/фото/документ)\n"
        "• Видео-выступление или клип\n"
        "• Любую другую творческую работу\n\n"
        "Твоя работа анонимно уйдёт всем участникам.\n"
        "Они смогут поставить оценку от 1 до 5, оставить комментарий, "
        "написать тебе или пожаловаться на неуместный контент.\n\n"
        "📤 Просто отправь файл или текст — и поехали!"
    )


async def receive_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.username)

    if is_blocked(user.id):
        await update.message.reply_text("❌ Вы заблокированы и не можете отправлять работы.")
        return

    msg = update.message
    file_id = None
    caption = None
    msg_type = None

    if msg.photo:
        file_id = msg.photo[-1].file_id
        caption = msg.caption
        msg_type = "photo"
    elif msg.video:
        file_id = msg.video.file_id
        caption = msg.caption
        msg_type = "video"
    elif msg.audio:
        file_id = msg.audio.file_id
        caption = msg.caption
        msg_type = "audio"
    elif msg.voice:
        file_id = msg.voice.file_id
        caption = msg.caption
        msg_type = "voice"
    elif msg.document:
        file_id = msg.document.file_id
        caption = msg.caption
        msg_type = "document"
    elif msg.text:
        caption = msg.text
        msg_type = "text"
    else:
        await msg.reply_text(
            "❓ Поддерживаются: аудио, голосовые, фото, видео, документы и текст."
        )
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO works (author_id, file_id, caption, msg_type) VALUES (?,?,?,?)",
        (user.id, file_id, caption, msg_type),
    )
    work_id = cur.lastrowid
    conn.commit()
    conn.close()

    reply_markup = build_work_keyboard(work_id)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id FROM users WHERE user_id != ? AND blocked = 0",
        (user.id,),
    )
    recipients = cur.fetchall()
    conn.close()

    sent = 0
    for (uid,) in recipients:
        try:
            await send_work_to_user(
                context, uid, msg_type, file_id, caption,
                msg.chat_id, msg.message_id, reply_markup,
            )
            sent += 1
        except Exception as e:
            logger.warning("Ошибка отправки пользователю %s: %s", uid, e)

    await msg.reply_text(
        f"✅ Работа #{work_id} отправлена на оценку!\n"
        f"👥 Получили: {sent} участник(ов)"
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    action = parts[0]

    # ── Admin block from complaint ──────────────────────────────────────────
    if action == "adminblock":
        if query.from_user.id != ADMIN_ID:
            await query.answer("Нет доступа.", show_alert=True)
            return
        target_id = int(parts[1])
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (target_id,))
        cur.execute("UPDATE users SET blocked = 1 WHERE user_id=?", (target_id,))
        conn.commit()
        conn.close()
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🚫 Пользователь {target_id} заблокирован."
        )
        return

    work_id = int(parts[1])
    voter = query.from_user.id

    # ── Rate ───────────────────────────────────────────────────────────────
    if action == "rate":
        score = int(parts[2])
        conn = get_conn()
        cur = conn.cursor()

        # Don't let the author rate their own work
        cur.execute("SELECT author_id FROM works WHERE work_id=?", (work_id,))
        row = cur.fetchone()
        if row and row[0] == voter:
            conn.close()
            await query.answer("Нельзя оценивать собственную работу.", show_alert=True)
            return

        cur.execute(
            "SELECT score FROM ratings WHERE work_id=? AND voter_id=?",
            (work_id, voter),
        )
        existing = cur.fetchone()
        if existing:
            conn.close()
            await query.answer(
                f"Вы уже оценили эту работу на {existing[0]}.", show_alert=True
            )
            return

        cur.execute(
            "INSERT INTO ratings (work_id, voter_id, score) VALUES (?,?,?)",
            (work_id, voter, score),
        )
        conn.commit()

        author_id = row[0] if row else None
        cur.execute("SELECT AVG(score), COUNT(*) FROM ratings WHERE work_id=?", (work_id,))
        avg_row = cur.fetchone()
        conn.close()

        if author_id:
            avg, count = avg_row
            await context.bot.send_message(
                chat_id=author_id,
                text=(
                    f"📊 Новая оценка вашей работы #{work_id}: {score} ⭐\n"
                    f"Средний балл: {avg:.1f} (всего оценок: {count})"
                ),
            )
        await query.edit_message_reply_markup(
            reply_markup=build_work_keyboard(work_id)
        )
        await query.answer(f"Вы поставили {score} ⭐ — спасибо!", show_alert=False)

    # ── Comment ────────────────────────────────────────────────────────────
    elif action == "comment":
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT author_id FROM works WHERE work_id=?", (work_id,))
        row = cur.fetchone()
        conn.close()
        if row and row[0] == voter:
            await query.answer("Нельзя комментировать собственную работу.", show_alert=True)
            return
        context.user_data["comment_work_id"] = work_id
        await query.message.reply_text(
            "📝 Напишите ваш комментарий к работе одним сообщением.\n"
            "(Например: что понравилось, что улучшить, почему такая оценка)"
        )
        return WAITING_COMMENT

    # ── Contact ────────────────────────────────────────────────────────────
    elif action == "contact":
        conn = get_conn()
        cur = conn.cursor()

        cur.execute("SELECT author_id FROM works WHERE work_id=?", (work_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return
        author_id = row[0]

        if author_id == voter:
            conn.close()
            await query.answer("Это ваша собственная работа.", show_alert=True)
            return

        # Prevent duplicate contact requests
        cur.execute(
            "SELECT 1 FROM contacts WHERE work_id=? AND voter_id=?",
            (work_id, voter),
        )
        if cur.fetchone():
            conn.close()
            await query.answer("Вы уже отправляли запрос на контакт.", show_alert=True)
            return

        cur.execute(
            "INSERT INTO contacts (work_id, voter_id) VALUES (?,?)",
            (work_id, voter),
        )
        conn.commit()
        conn.close()

        username = query.from_user.username
        full_name = query.from_user.full_name or ""
        if username:
            contact_info = f"👤 @{username} ({full_name}) хочет с вами связаться по работе #{work_id}."
        else:
            contact_info = (
                f"👤 Пользователь «{full_name}» (ID: {voter}) хочет с вами связаться "
                f"по работе #{work_id}.\n"
                f"Напишите им: tg://user?id={voter}"
            )
        await context.bot.send_message(chat_id=author_id, text=contact_info)
        await query.answer("✅ Запрос на контакт отправлен автору.", show_alert=True)

    # ── Complain ────────────────────────────────────────────────────────────
    elif action == "complain":
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT author_id, file_id, caption, msg_type FROM works WHERE work_id=?",
            (work_id,),
        )
        row = cur.fetchone()
        conn.close()

        if not row:
            await query.answer("Работа не найдена.", show_alert=True)
            return

        author_id, file_id, caption, msg_type = row

        if author_id == voter:
            await query.answer("Нельзя жаловаться на собственную работу.", show_alert=True)
            return

        complaint_text = (
            f"⚠️ ЖАЛОБА на работу #{work_id}\n"
            f"Автор ID: {author_id}\n"
            f"Тип: {msg_type}\n"
        )
        if caption:
            complaint_text += f"Подпись/текст: {caption}\n"
        complaint_text += f"\nИспользуйте кнопку ниже или /block {author_id} для блокировки."

        try:
            await forward_work_to_admin(
                context, work_id, author_id, msg_type or "text",
                file_id, caption,
                query.message.chat_id, query.message.message_id,
                complaint_text,
            )
        except Exception as e:
            logger.error("Ошибка пересылки жалобы админу: %s", e)

        await query.answer("✅ Жалоба отправлена администратору.", show_alert=True)


async def receive_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    work_id = context.user_data.get("comment_work_id")
    if not work_id:
        return ConversationHandler.END

    comment = update.message.text
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT author_id FROM works WHERE work_id=?", (work_id,))
    result = cur.fetchone()
    conn.close()

    if result:
        author_id = result[0]
        sender = update.effective_user
        await context.bot.send_message(
            chat_id=author_id,
            text=(
                f"💬 Новый комментарий к вашей работе #{work_id}:\n\n"
                f"{comment}"
            ),
        )
    await update.message.reply_text("✅ Комментарий отправлен автору. Спасибо!")
    context.user_data.pop("comment_work_id", None)
    return ConversationHandler.END


# ─── Admin commands ────────────────────────────────────────────────────────────

async def block_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Нет доступа.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /block <user_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id должен быть числом.")
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (target_id,))
    cur.execute("UPDATE users SET blocked = 1 WHERE user_id=?", (target_id,))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"🚫 Пользователь {target_id} заблокирован.")


async def unblock_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Нет доступа.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /unblock <user_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id должен быть числом.")
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET blocked = 0 WHERE user_id=?", (target_id,))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Пользователь {target_id} разблокирован.")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Нет доступа.")
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users WHERE blocked=1")
    blocked_users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM works")
    total_works = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM ratings")
    total_ratings = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM contacts")
    total_contacts = cur.fetchone()[0]
    conn.close()
    await update.message.reply_text(
        f"📈 Статистика бота:\n\n"
        f"👥 Пользователей: {total_users} (заблокировано: {blocked_users})\n"
        f"🎵 Работ отправлено: {total_works}\n"
        f"⭐ Оценок поставлено: {total_ratings}\n"
        f"🤝 Запросов на контакт: {total_contacts}"
    )


async def top_works(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT w.work_id, AVG(r.score) as avg, COUNT(r.score) as cnt "
        "FROM works w JOIN ratings r ON w.work_id = r.work_id "
        "GROUP BY w.work_id HAVING cnt >= 1 "
        "ORDER BY avg DESC, cnt DESC LIMIT 10"
    )
    rows = cur.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("Пока нет ни одной оценённой работы.")
        return
    text = "🏆 Топ-10 работ по оценкам:\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, (work_id, avg, cnt) in enumerate(rows):
        medal = medals[i] if i < 3 else f"{i+1}."
        text += f"{medal} Работа #{work_id} — {avg:.1f} ⭐ ({cnt} оценок)\n"
    await update.message.reply_text(text)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("top",   top_works))
    app.add_handler(CommandHandler("block",   block_user))
    app.add_handler(CommandHandler("unblock", unblock_user))
    app.add_handler(CommandHandler("stats",   stats))

    # Comment conversation: triggered by the "Комментарий" button
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
    app.add_handler(conv_handler)

    # All other inline buttons
    app.add_handler(
        CallbackQueryHandler(
            button_handler,
            pattern=r"^(rate|contact|complain|adminblock)_",
        )
    )

    # Incoming works (any media or text that isn't a command)
    app.add_handler(
        MessageHandler(
            (
                filters.PHOTO
                | filters.VIDEO
                | filters.AUDIO
                | filters.VOICE
                | filters.Document.ALL
                | filters.TEXT
            )
            & ~filters.COMMAND,
            receive_work,
        )
    )

    logger.info("Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
