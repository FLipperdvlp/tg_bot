import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import cv2
import numpy as np
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

DB_FILE = os.getenv("DB_FILE", "bot.db")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8863892337:AAF3CuoRciPKZW1jq0sM_HEg1WcpxVx0KVY").strip()
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "Ugiqatemu").strip().lstrip("@").lower()
DEFAULT_HOLD_MINUTES = int(os.getenv("DEFAULT_HOLD_MINUTES", "10"))
MAX_HOLD_MINUTES = 1440

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# -------------------- Render health server --------------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if urlparse(self.path).path in ("/", "/health"):
            body = b"OK"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health server listening on port %s", port)


# -------------------- Database --------------------
def get_db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            enabled INTEGER NOT NULL DEFAULT 0,
            hold_minutes INTEGER NOT NULL DEFAULT 10
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS qr_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            display_name TEXT,
            sent_at TEXT NOT NULL,
            counted INTEGER NOT NULL DEFAULT 0,
            qr_data TEXT,
            domain TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_cache (
            user_id INTEGER PRIMARY KEY,
            username TEXT
        )
    """)

    # Upgrade older database created by the previous bot.
    for statement in (
        "ALTER TABLE qr_messages ADD COLUMN qr_data TEXT",
        "ALTER TABLE qr_messages ADD COLUMN domain TEXT",
    ):
        try:
            cur.execute(statement)
        except sqlite3.OperationalError:
            pass

    conn.commit()
    conn.close()


def ensure_group(chat_id: int, title: str | None = None):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO groups (chat_id, title, enabled, hold_minutes) VALUES (?, ?, 0, ?)",
        (chat_id, title, DEFAULT_HOLD_MINUTES),
    )
    if title:
        cur.execute("UPDATE groups SET title = ? WHERE chat_id = ?", (title, chat_id))
    conn.commit()
    conn.close()


def get_group_settings(chat_id: int):
    ensure_group(chat_id)
    conn = get_db()
    row = conn.execute("SELECT * FROM groups WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    return row


def get_all_groups():
    conn = get_db()
    rows = conn.execute("SELECT * FROM groups ORDER BY title").fetchall()
    conn.close()
    return rows


# -------------------- Admins --------------------
def normalize_username(username: str) -> str:
    return (username or "").strip().lstrip("@").lower()


def is_admin(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    if OWNER_USERNAME and normalize_username(user.username) == OWNER_USERNAME:
        return True
    conn = get_db()
    row = conn.execute("SELECT user_id FROM admins WHERE user_id = ?", (user.id,)).fetchone()
    conn.close()
    return row is not None


def register_owner(update: Update):
    user = update.effective_user
    if not user or not OWNER_USERNAME or normalize_username(user.username) != OWNER_USERNAME:
        return
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO admins (user_id, username) VALUES (?, ?)",
        (user.id, normalize_username(user.username)),
    )
    conn.commit()
    conn.close()


def get_admins():
    conn = get_db()
    rows = conn.execute("SELECT user_id, username FROM admins ORDER BY username").fetchall()
    conn.close()
    return rows


def add_admin(user_id: int, username: str):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO admins (user_id, username) VALUES (?, ?)",
        (user_id, normalize_username(username)),
    )
    conn.commit()
    conn.close()


def remove_admin(user_id: int):
    conn = get_db()
    conn.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


# -------------------- User/group cache --------------------
async def cache_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO user_cache (user_id, username) VALUES (?, ?)",
        (user.id, normalize_username(user.username)),
    )
    conn.commit()
    conn.close()


async def register_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        ensure_group(chat.id, chat.title or "Без названия")


# -------------------- QR detection --------------------
def is_max_qr(data: str) -> bool:
    """Return True only for QR payloads belonging to max.ru."""
    if not data:
        return False
    value = data.strip().lower()
    if not value:
        return False

    # Accept normal URLs such as https://max.ru/... or https://sub.max.ru/...
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if host == "max.ru" or host.endswith(".max.ru"):
            return True
    except ValueError:
        pass

    # Some QR codes can contain a plain max.ru string rather than a full URL.
    return value.startswith("max.ru/") or value == "max.ru"


def decode_qr_from_bytes(image_bytes: bytes) -> list[str]:
    array = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        return []

    detector = cv2.QRCodeDetector()
    results: list[str] = []

    try:
        ok, decoded_info, points, _ = detector.detectAndDecodeMulti(image)
        if ok and decoded_info:
            results.extend([x for x in decoded_info if x])
    except Exception:
        pass

    if not results:
        try:
            data, _, _ = detector.detectAndDecode(image)
            if data:
                results.append(data)
        except Exception:
            pass

    # Remove duplicates while preserving order.
    return list(dict.fromkeys(results))


# -------------------- Statistics --------------------
def get_group_statistics(chat_id: int):
    conn = get_db()
    users = conn.execute("""
        SELECT user_id, username, display_name, COUNT(*) AS total
        FROM qr_messages
        WHERE chat_id = ? AND counted = 1
        GROUP BY user_id
        ORDER BY total DESC
    """, (chat_id,)).fetchall()
    total_all = conn.execute(
        "SELECT COUNT(*) AS total FROM qr_messages WHERE chat_id = ?", (chat_id,)
    ).fetchone()["total"]
    total_counted = conn.execute(
        "SELECT COUNT(*) AS total FROM qr_messages WHERE chat_id = ? AND counted = 1", (chat_id,)
    ).fetchone()["total"]
    users_count = conn.execute(
        "SELECT COUNT(DISTINCT user_id) AS total FROM qr_messages WHERE chat_id = ?", (chat_id,)
    ).fetchone()["total"]
    conn.close()
    return users, total_all, total_counted, users_count


# -------------------- Keyboards --------------------
def groups_menu():
    keyboard = []
    for group in get_all_groups():
        title = group["title"] or "Без названия"
        status = "🟢" if group["enabled"] else "🔴"
        keyboard.append([InlineKeyboardButton(f"{status} {title}", callback_data=f"group:{group['chat_id']}")])
    keyboard += [
        [InlineKeyboardButton("👥 Администраторы", callback_data="admins")],
        [InlineKeyboardButton("🔄 Обновить", callback_data="groups")],
    ]
    return InlineKeyboardMarkup(keyboard)


def group_menu(chat_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика", callback_data=f"stats:{chat_id}")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data=f"settings:{chat_id}")],
        [
            InlineKeyboardButton("🟢 Включить", callback_data=f"enable:{chat_id}"),
            InlineKeyboardButton("🔴 Выключить", callback_data=f"disable:{chat_id}"),
        ],
        [InlineKeyboardButton("⏱ Изменить Hold", callback_data=f"hold:{chat_id}")],
        [InlineKeyboardButton("🗑 Очистить статистику", callback_data=f"clear:{chat_id}")],
        [InlineKeyboardButton("🔄 Обновить", callback_data=f"group:{chat_id}")],
        [InlineKeyboardButton("⬅️ К группам", callback_data="groups")],
    ])


def admins_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить администратора", callback_data="add_admin")],
        [InlineKeyboardButton("➖ Удалить администратора", callback_data="remove_admin")],
        [InlineKeyboardButton("📋 Список администраторов", callback_data="admins_list")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="groups")],
    ])


# -------------------- Panel text --------------------
def make_group_overview(chat_id: int):
    row = get_group_settings(chat_id)
    users, total_all, total_counted, users_count = get_group_statistics(chat_id)
    status = "🟢 Включено" if row["enabled"] else "🔴 Выключено"
    return (
        f"📱 <b>{row['title'] or 'Без названия'}</b>\n\n"
        f"Статус: {status}\n"
        f"⏱ Hold: <b>{row['hold_minutes']} мин.</b>\n"
        f"🔗 QR только: <b>max.ru</b>\n\n"
        f"👥 Участников: <b>{users_count}</b>\n"
        f"🔎 QR max.ru: <b>{total_all}</b>\n"
        f"✅ Засчитано: <b>{total_counted}</b>\n"
        f"❌ Hold: <b>{total_all - total_counted}</b>"
    )


def make_stats_text(chat_id: int):
    users, total_all, total_counted, users_count = get_group_statistics(chat_id)
    title = get_group_settings(chat_id)["title"] or "Без названия"
    text = f"📊 <b>Статистика</b>\n📱 {title}\n🔗 Только QR max.ru\n\n"
    if not users:
        return text + "Пока нет засчитанных QR."
    for i, user in enumerate(users, 1):
        name = f"@{user['username']}" if user["username"] else user["display_name"]
        text += f"<b>{i}.</b> {name} — <b>{user['total']}</b> QR\n"
    text += (
        f"\n━━━━━━━━━━━━━━\n"
        f"📨 Всего QR: <b>{total_all}</b>\n"
        f"✅ Засчитано: <b>{total_counted}</b>\n"
        f"❌ Hold: <b>{total_all - total_counted}</b>\n"
        f"👥 Участников: <b>{users_count}</b>"
    )
    return text


# -------------------- Commands --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_owner(update)
    if update.effective_chat.type != "private":
        await update.message.reply_text("Используйте команды в группе.")
        return
    if not is_admin(update):
        await update.message.reply_text("❌ У вас нет доступа к панели администратора.")
        return
    groups = get_all_groups()
    if not groups:
        await update.message.reply_text(
            "🔧 <b>Панель администратора</b>\n\n"
            "Бот пока не обнаружил ни одной группы.\n"
            "Добавьте бота в группу и отправьте там любое сообщение.",
            parse_mode="HTML",
        )
        return
    await update.message.reply_text(
        "🔧 <b>Панель администратора</b>\n\nВыберите группу:",
        parse_mode="HTML",
        reply_markup=groups_menu(),
    )


async def simple_group_command(update: Update, enabled: bool):
    register_owner(update)
    if not is_admin(update):
        await update.message.reply_text("❌ У вас нет прав администратора бота.")
        return
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("❌ Эту команду нужно использовать в группе.")
        return
    ensure_group(chat.id, chat.title)
    conn = get_db()
    conn.execute("UPDATE groups SET enabled = ? WHERE chat_id = ?", (1 if enabled else 0, chat.id))
    conn.commit()
    conn.close()
    await update.message.reply_text("🟢 Сканирование включено." if enabled else "🔴 Сканирование выключено.")


async def enable_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await simple_group_command(update, True)


async def disable_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await simple_group_command(update, False)


async def set_hold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_owner(update)
    if not is_admin(update):
        await update.message.reply_text("❌ У вас нет прав администратора бота.")
        return
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("❌ Эту команду нужно использовать в группе.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /sethold 10")
        return
    try:
        minutes = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Введите целое число минут.")
        return
    if not 0 <= minutes <= MAX_HOLD_MINUTES:
        await update.message.reply_text(f"❌ Hold должен быть от 0 до {MAX_HOLD_MINUTES} минут.")
        return
    ensure_group(chat.id, chat.title)
    conn = get_db()
    conn.execute("UPDATE groups SET hold_minutes = ? WHERE chat_id = ?", (minutes, chat.id))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Hold установлен: {minutes} минут.")


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("❌ Эту команду нужно использовать в группе.")
        return
    row = get_group_settings(chat.id)
    status = "🟢 Включено" if row["enabled"] else "🔴 Выключено"
    await update.message.reply_text(
        f"⚙️ Настройки\n\nСтатус: {status}\n⏱ Hold: {row['hold_minutes']} минут\n🔗 QR: только max.ru"
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("❌ Эту команду нужно использовать в группе.")
        return
    ensure_group(chat.id, chat.title)
    await update.message.reply_text(make_stats_text(chat.id), parse_mode="HTML")


async def clear_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_owner(update)
    if not is_admin(update):
        await update.message.reply_text("❌ У вас нет прав администратора бота.")
        return
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("❌ Эту команду нужно использовать в группе.")
        return
    conn = get_db()
    conn.execute("DELETE FROM qr_messages WHERE chat_id = ?", (chat.id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("🗑 Статистика очищена.")


# -------------------- Admin callbacks --------------------
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if not is_admin(update):
        await query.edit_message_text("❌ У вас больше нет доступа.")
        return

    data = query.data
    if data == "groups":
        await query.edit_message_text("📱 <b>Выберите группу</b>", parse_mode="HTML", reply_markup=groups_menu())
        return

    if data.startswith("group:"):
        chat_id = int(data.split(":", 1)[1])
        await query.edit_message_text(make_group_overview(chat_id), parse_mode="HTML", reply_markup=group_menu(chat_id))
        return

    if data.startswith("stats:"):
        chat_id = int(data.split(":", 1)[1])
        await query.edit_message_text(make_stats_text(chat_id), parse_mode="HTML", reply_markup=group_menu(chat_id))
        return

    if data.startswith("settings:"):
        chat_id = int(data.split(":", 1)[1])
        row = get_group_settings(chat_id)
        status = "🟢 Включено" if row["enabled"] else "🔴 Выключено"
        await query.edit_message_text(
            f"⚙️ <b>Настройки</b>\n\nСтатус: {status}\n⏱ Hold: <b>{row['hold_minutes']} мин.</b>\n🔗 QR: <b>только max.ru</b>",
            parse_mode="HTML", reply_markup=group_menu(chat_id)
        )
        return

    if data.startswith("enable:") or data.startswith("disable:"):
        chat_id = int(data.split(":", 1)[1])
        enabled = data.startswith("enable:")
        conn = get_db()
        conn.execute("UPDATE groups SET enabled = ? WHERE chat_id = ?", (1 if enabled else 0, chat_id))
        conn.commit(); conn.close()
        await query.edit_message_text(make_group_overview(chat_id), parse_mode="HTML", reply_markup=group_menu(chat_id))
        return

    if data.startswith("hold:"):
        chat_id = int(data.split(":", 1)[1])
        context.user_data["waiting_hold"] = chat_id
        await query.edit_message_text(
            "⏱ <b>Изменение Hold</b>\n\nОтправьте количество минут.\nНапример: <code>10</code>\n\nМаксимум — 1440 минут.",
            parse_mode="HTML"
        )
        return

    if data.startswith("clear:"):
        chat_id = int(data.split(":", 1)[1])
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Да, очистить", callback_data=f"confirm_clear:{chat_id}")],
            [InlineKeyboardButton("⬅️ Отмена", callback_data=f"group:{chat_id}")],
        ])
        await query.edit_message_text("⚠️ <b>Очистить статистику?</b>\n\nВсе QR этой группы будут удалены.", parse_mode="HTML", reply_markup=keyboard)
        return

    if data.startswith("confirm_clear:"):
        chat_id = int(data.split(":", 1)[1])
        conn = get_db(); conn.execute("DELETE FROM qr_messages WHERE chat_id = ?", (chat_id,)); conn.commit(); conn.close()
        await query.edit_message_text("✅ <b>Статистика очищена.</b>", parse_mode="HTML", reply_markup=group_menu(chat_id))
        return

    if data == "admins":
        await query.edit_message_text("👥 <b>Управление администраторами</b>", parse_mode="HTML", reply_markup=admins_menu())
        return

    if data == "admins_list":
        admins = get_admins()
        owner = OWNER_USERNAME
        text = "👥 <b>Администраторы</b>\n\n"
        if not admins:
            text += "Список пока пуст."
        else:
            for i, admin in enumerate(admins, 1):
                crown = " 👑" if admin["username"] == owner else ""
                text += f"{i}. @{admin['username']}{crown}\n"
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=admins_menu())
        return

    if data == "add_admin":
        context.user_data["waiting_for_admin"] = True
        await query.edit_message_text("➕ Отправьте @username пользователя, которого нужно добавить.")
        return

    if data == "remove_admin":
        context.user_data["waiting_for_remove_admin"] = True
        await query.edit_message_text("➖ Отправьте @username администратора, которого нужно удалить.")
        return


async def handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_admin(update):
        return
    text = (update.message.text or "").strip()

    if "waiting_hold" in context.user_data:
        chat_id = context.user_data.pop("waiting_hold")
        try:
            minutes = int(text)
        except ValueError:
            await update.message.reply_text("❌ Введите целое число минут.")
            context.user_data["waiting_hold"] = chat_id
            return
        if not 0 <= minutes <= MAX_HOLD_MINUTES:
            await update.message.reply_text(f"❌ Hold должен быть от 0 до {MAX_HOLD_MINUTES} минут.")
            context.user_data["waiting_hold"] = chat_id
            return
        conn = get_db(); conn.execute("UPDATE groups SET hold_minutes = ? WHERE chat_id = ?", (minutes, chat_id)); conn.commit(); conn.close()
        await update.message.reply_text(f"✅ Hold установлен: {minutes} минут.", reply_markup=group_menu(chat_id))
        return

    if context.user_data.get("waiting_for_admin"):
        context.user_data["waiting_for_admin"] = False
        username = normalize_username(text)
        conn = get_db(); row = conn.execute("SELECT user_id FROM user_cache WHERE username = ?", (username,)).fetchone(); conn.close()
        if not username or not row:
            await update.message.reply_text("❌ Пользователь не найден. Пусть он сначала отправит боту /start или любое сообщение.")
            return
        if username == OWNER_USERNAME:
            await update.message.reply_text("ℹ️ Это главный администратор.")
            return
        add_admin(row["user_id"], username)
        await update.message.reply_text(f"✅ <b>@{username}</b> добавлен в администраторы.", parse_mode="HTML", reply_markup=admins_menu())
        return

    if context.user_data.get("waiting_for_remove_admin"):
        context.user_data["waiting_for_remove_admin"] = False
        username = normalize_username(text)
        conn = get_db(); row = conn.execute("SELECT user_id FROM admins WHERE username = ?", (username,)).fetchone(); conn.close()
        if not row:
            await update.message.reply_text("❌ Такой администратор не найден.")
            return
        if username == OWNER_USERNAME:
            await update.message.reply_text("❌ Нельзя удалить главного администратора.")
            return
        remove_admin(row["user_id"])
        await update.message.reply_text(f"✅ @{username} удалён из администраторов.", reply_markup=admins_menu())


# -------------------- QR handler --------------------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user or chat.type not in ("group", "supergroup"):
        return

    settings_row = get_group_settings(chat.id)
    if not settings_row["enabled"]:
        return

    # Download the highest-resolution Telegram photo.
    try:
        photo = message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await tg_file.download_as_bytearray())
    except Exception:
        logger.exception("Could not download photo")
        return

    decoded = decode_qr_from_bytes(image_bytes)
    max_codes = [data for data in decoded if is_max_qr(data)]

    # A random screenshot/photo is NOT a QR and is NOT counted.
    if not max_codes:
        logger.info("Photo from %s ignored: no max.ru QR", user.id)
        return

    now = datetime.now(timezone.utc)
    hold_seconds = settings_row["hold_minutes"] * 60
    conn = get_db()

    # IMPORTANT: hold is separate for each user AND each group.
    last = conn.execute("""
        SELECT sent_at FROM qr_messages
        WHERE chat_id = ? AND user_id = ? AND counted = 1
        ORDER BY id DESC LIMIT 1
    """, (chat.id, user.id)).fetchone()

    counted = False
    if last is None:
        counted = True
    else:
        try:
            last_time = datetime.fromisoformat(last["sent_at"])
            if (now - last_time).total_seconds() >= hold_seconds:
                counted = True
        except ValueError:
            counted = True

    username = normalize_username(user.username)
    display_name = user.full_name or username or str(user.id)

    # One Telegram image can contain multiple QR codes. Count the message once,
    # while saving the first matching max.ru payload for diagnostics.
    qr_data = max_codes[0]
    domain = urlparse(qr_data).hostname or "max.ru"

    conn.execute("""
        INSERT INTO qr_messages
        (chat_id, user_id, username, display_name, sent_at, counted, qr_data, domain)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        chat.id, user.id, username, display_name, now.isoformat(),
        1 if counted else 0, qr_data, domain,
    ))
    conn.commit(); conn.close()

    logger.info(
        "%s max.ru QR: %s (%s)",
        "COUNTED" if counted else "HOLD",
        display_name,
        user.id,
    )


# -------------------- Delete ordinary group messages --------------------
async def delete_non_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat = update.effective_chat
    if not message or not chat or chat.type not in ("group", "supergroup"):
        return
    if message.photo or message.text and message.text.startswith("/"):
        return
    try:
        await message.delete()
    except Exception as error:
        logger.warning("Не удалось удалить сообщение: %s", error)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling update:", exc_info=context.error)


# -------------------- Main --------------------
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    if not OWNER_USERNAME:
        raise RuntimeError("OWNER_USERNAME is not set")

    init_db()
    start_health_server()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("on", enable_bot))
    application.add_handler(CommandHandler("off", disable_bot))
    application.add_handler(CommandHandler("sethold", set_hold))
    application.add_handler(CommandHandler("settings", settings))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("clear", clear_stats))
    application.add_handler(CallbackQueryHandler(admin_panel))

    application.add_handler(MessageHandler(filters.ALL, register_group), group=5)
    application.add_handler(MessageHandler(filters.ALL, cache_user), group=10)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_input), group=1)
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo), group=0)
    application.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND & ~filters.PHOTO, delete_non_command),
        group=20,
    )
    application.add_error_handler(error_handler)

    logger.info("Bot started")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
