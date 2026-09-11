import logging
import io
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone

import cv2
import numpy as np
import psycopg2
import psycopg2.extras

from telegram.error import BadRequest
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from config import BOT_TOKEN, OWNER_USERNAME


TECHNICAL_BREAK_TEXT = (
    "🛠 <b>Технический перерыв</b>\n\n"
    "Бот отключён на время технического перерыва.\n"
    "Сканирование отключено.\n\n"
    "❗ Пожалуйста, не отправляйте QR-коды "
    "до окончания технического перерыва.\n"
    "Перерыв продлится до 10 минут."
)


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# =========================================================
# POSTGRESQL
# =========================================================

DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL не задана. "
        "Добавь DATABASE_URL в Environment Variables Render."
    )


def get_db():
    """
    Создаёт новое подключение к PostgreSQL.
    """
    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=psycopg2.extras.RealDictCursor,
        connect_timeout=10,
    )


def init_db():
    """
    Создаёт необходимые таблицы и индексы.
    Повторный запуск безопасен.
    """

    conn = get_db()

    try:
        cursor = conn.cursor()

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS groups (
                chat_id BIGINT PRIMARY KEY,
                title TEXT,
                enabled BOOLEAN NOT NULL DEFAULT FALSE,
                hold_minutes INTEGER NOT NULL DEFAULT 10
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS qr_messages (
                id BIGSERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                username TEXT,
                display_name TEXT,
                qr_data TEXT,
                sent_at TIMESTAMPTZ NOT NULL,
                counted BOOLEAN NOT NULL DEFAULT FALSE
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                user_id BIGINT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS user_cache (
                user_id BIGINT PRIMARY KEY,
                username TEXT
            )
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_qr_chat_user_counted
            ON qr_messages (
                chat_id,
                user_id,
                counted,
                id DESC
            )
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_qr_chat
            ON qr_messages (chat_id)
            """
        )

        conn.commit()

        logging.info(
            "PostgreSQL database initialized successfully."
        )

    except Exception:
        conn.rollback()
        logging.exception(
            "Ошибка инициализации PostgreSQL."
        )
        raise

    finally:
        conn.close()

# =========================================================
# RENDER HEALTH SERVER
# =========================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8",
        )
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return


def start_render_health_server():
    port = int(os.environ.get("PORT", "10000"))

    server = HTTPServer(
        ("0.0.0.0", port),
        HealthHandler,
    )

    logging.info(
        "Render health server started on port %s",
        port,
    )

    server.serve_forever()


# =========================================================
# GROUPS
# =========================================================

def ensure_group(chat_id: int, title: str = None):

    conn = get_db()

    try:
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO groups
                (chat_id, title, enabled, hold_minutes)
            VALUES
                (%s, %s, 0, 10)
            ON CONFLICT (chat_id)
            DO NOTHING
            """,
            (
                chat_id,
                title,
            ),
        )

        if title:
            cursor.execute(
                """
                UPDATE groups
                SET title = %s
                WHERE chat_id = %s
                """,
                (
                    title,
                    chat_id,
                ),
            )

        conn.commit()

    except Exception:
        conn.rollback()
        logging.exception("Ошибка ensure_group")

    finally:
        conn.close()


def get_group_settings(chat_id: int):

    ensure_group(chat_id)

    conn = get_db()

    try:
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT *
            FROM groups
            WHERE chat_id = %s
            """,
            (chat_id,),
        )

        return cursor.fetchone()

    finally:
        conn.close()


def get_all_groups():

    conn = get_db()

    try:
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT *
            FROM groups
            ORDER BY title
            """
        )

        return cursor.fetchall()

    finally:
        conn.close()


# =========================================================
# ADMIN
# =========================================================

def normalize_username(username: str) -> str:
    return username.strip().lstrip("@").lower()


def is_admin(update: Update) -> bool:

    user = update.effective_user

    if not user:
        return False

    if not user.username:
        return False

    username = normalize_username(user.username)
    owner = normalize_username(OWNER_USERNAME)

    if username == owner:
        return True

    conn = get_db()

    try:
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT user_id
            FROM admins
            WHERE user_id = %s
            """,
            (user.id,),
        )

        row = cursor.fetchone()

        return row is not None

    finally:
        conn.close()


def register_owner(update: Update):

    user = update.effective_user

    if not user or not user.username:
        return

    if normalize_username(user.username) != normalize_username(
        OWNER_USERNAME
    ):
        return

    conn = get_db()

    try:
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO admins
                (user_id, username)
            VALUES
                (%s, %s)
            ON CONFLICT (user_id)
            DO UPDATE SET
                username = EXCLUDED.username
            """,
            (
                user.id,
                normalize_username(user.username),
            ),
        )

        conn.commit()

    except Exception:
        conn.rollback()
        logging.exception("Ошибка register_owner")

    finally:
        conn.close()


def get_admins():

    conn = get_db()

    try:
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT user_id, username
            FROM admins
            ORDER BY username
            """
        )

        return cursor.fetchall()

    finally:
        conn.close()


def add_admin(user_id: int, username: str):

    conn = get_db()

    try:
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO admins
                (user_id, username)
            VALUES
                (%s, %s)
            ON CONFLICT (user_id)
            DO UPDATE SET
                username = EXCLUDED.username
            """,
            (
                user_id,
                normalize_username(username),
            ),
        )

        conn.commit()

    except Exception:
        conn.rollback()
        logging.exception("Ошибка add_admin")

    finally:
        conn.close()


def remove_admin(user_id: int):

    conn = get_db()

    try:
        cursor = conn.cursor()

        cursor.execute(
            """
            DELETE FROM admins
            WHERE user_id = %s
            """,
            (user_id,),
        )

        conn.commit()

    except Exception:
        conn.rollback()
        logging.exception("Ошибка remove_admin")

    finally:
        conn.close()


# =========================================================
# USER CACHE
# =========================================================

async def cache_user(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if not user or not user.username:
        return

    conn = get_db()

    try:
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO user_cache
                (user_id, username)
            VALUES
                (%s, %s)
            ON CONFLICT (user_id)
            DO UPDATE SET
                username = EXCLUDED.username
            """,
            (
                user.id,
                normalize_username(user.username),
            ),
        )

        conn.commit()

    except Exception:
        conn.rollback()
        logging.exception("Ошибка cache_user")

    finally:
        conn.close()


# =========================================================
# REGISTER GROUP
# =========================================================

async def register_group(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat = update.effective_chat

    if not chat:
        return

    if chat.type not in ("group", "supergroup"):
        return

    title = chat.title or "Без названия"

    ensure_group(
        chat.id,
        title,
    )


# =========================================================
# STATISTICS
# =========================================================

def get_group_statistics(chat_id: int):

    conn = get_db()

    try:
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT
                user_id,
                username,
                display_name,
                COUNT(*) AS total
            FROM qr_messages
            WHERE chat_id = %s
              AND counted = 1
            GROUP BY
                user_id,
                username,
                display_name
            ORDER BY total DESC
            """,
            (chat_id,),
        )

        users = cursor.fetchall()

        cursor.execute(
            """
            SELECT COUNT(*) AS total
            FROM qr_messages
            WHERE chat_id = %s
            """,
            (chat_id,),
        )

        total_all = cursor.fetchone()["total"]

        cursor.execute(
            """
            SELECT COUNT(*) AS total
            FROM qr_messages
            WHERE chat_id = %s
              AND counted = 1
            """,
            (chat_id,),
        )

        total_counted = cursor.fetchone()["total"]

        cursor.execute(
            """
            SELECT COUNT(DISTINCT user_id) AS total
            FROM qr_messages
            WHERE chat_id = %s
            """,
            (chat_id,),
        )

        users_count = cursor.fetchone()["total"]

        return (
            users,
            total_all,
            total_counted,
            users_count,
        )

    finally:
        conn.close()


# =========================================================
# MENUS
# =========================================================

def groups_menu():

    groups = get_all_groups()

    keyboard = []

    for group in groups:

        title = group["title"] or "Без названия"

        status = (
            "🟢"
            if group["enabled"]
            else "🔴"
        )

        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{status} {title}",
                    callback_data=f"group:{group['chat_id']}",
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                "👥 Администраторы",
                callback_data="admins",
            )
        ]
    )

    keyboard.append(
        [
            InlineKeyboardButton(
                "🔄 Обновить",
                callback_data="groups",
            )
        ]
    )

    return InlineKeyboardMarkup(keyboard)


def group_menu(chat_id: int):

    keyboard = [
        [
            InlineKeyboardButton(
                "📊 Статистика",
                callback_data=f"stats:{chat_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "⚙️ Настройки",
                callback_data=f"settings:{chat_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🟢 Включить",
                callback_data=f"enable:{chat_id}",
            ),
            InlineKeyboardButton(
                "🔴 Выключить",
                callback_data=f"disable:{chat_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "⏱ Изменить Hold",
                callback_data=f"hold:{chat_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🗑 Очистить статистику",
                callback_data=f"clear:{chat_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔄 Обновить",
                callback_data=f"group:{chat_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "⬅️ К группам",
                callback_data="groups",
            ),
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


def admins_menu():

    keyboard = [
        [
            InlineKeyboardButton(
                "➕ Добавить администратора",
                callback_data="add_admin",
            )
        ],
        [
            InlineKeyboardButton(
                "➖ Удалить администратора",
                callback_data="remove_admin",
            )
        ],
        [
            InlineKeyboardButton(
                "📋 Список администраторов",
                callback_data="admins_list",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data="groups",
            )
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


def admin_menu():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "👥 Группы",
                    callback_data="groups",
                )
            ],
            [
                InlineKeyboardButton(
                    "👥 Администраторы",
                    callback_data="admins",
                )
            ],
        ]
    )


# =========================================================
# TEXT
# =========================================================

def make_group_overview(chat_id: int):

    row = get_group_settings(chat_id)

    title = row["title"] or "Без названия"

    status = (
        "🟢 Включено"
        if row["enabled"]
        else "🔴 Выключено"
    )

    (
        users,
        total_all,
        total_counted,
        users_count,
    ) = get_group_statistics(chat_id)

    return (
        f"📱 <b>{title}</b>\n\n"
        f"Статус: {status}\n"
        f"⏱ Hold: <b>{row['hold_minutes']} мин.</b>\n\n"
        f"👥 Участников: <b>{users_count}</b>\n"
        f"📨 Фото отправлено: <b>{total_all}</b>\n"
        f"✅ Засчитано: <b>{total_counted}</b>\n"
        f"❌ Не прошло hold: "
        f"<b>{total_all - total_counted}</b>"
    )


def make_stats_text(chat_id: int):

    row = get_group_settings(chat_id)

    title = row["title"] or "Без названия"

    (
        users,
        total_all,
        total_counted,
        users_count,
    ) = get_group_statistics(chat_id)

    text = (
        "📊 <b>Статистика</b>\n"
        f"📱 {title}\n\n"
    )

    if not users:
        text += "Пока нет отправленных QR."
        return text

    for index, user in enumerate(users, start=1):

        if user["username"]:
            name = f"@{user['username']}"
        else:
            name = user["display_name"]

        text += (
            f"<b>{index}.</b> {name} — "
            f"<b>{user['total']}</b> QR\n"
        )

    text += (
        "\n━━━━━━━━━━━━━━\n"
        f"📨 Всего: <b>{total_all}</b>\n"
        f"✅ Засчитано: <b>{total_counted}</b>\n"
        f"❌ Hold: <b>{total_all - total_counted}</b>\n"
        f"👥 Участников: <b>{users_count}</b>"
    )

    return text


# =========================================================
# SAFE EDIT
# =========================================================

async def safe_edit_message(
    query,
    text,
    reply_markup=None,
):

    try:

        await query.edit_message_text(
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    except BadRequest as error:

        if "Message is not modified" not in str(error):
            raise


# =========================================================
# /START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    register_owner(update)

    if update.effective_chat.type != "private":

        await update.message.reply_text(
            "Используйте команды в группе."
        )

        return

    if not is_admin(update):

        await update.message.reply_text(
            "❌ У вас нет доступа к панели администратора."
        )

        return

    groups = get_all_groups()

    if not groups:

        await update.message.reply_text(
            "🔧 <b>Панель администратора</b>\n\n"
            "Бот пока не обнаружил ни одной группы.\n\n"
            "Добавьте бота в группу и отправьте там "
            "любое сообщение.",
            parse_mode="HTML",
        )

        return

    await update.message.reply_text(
        "🔧 <b>Панель администратора</b>\n\n"
        "Выберите группу:",
        parse_mode="HTML",
        reply_markup=groups_menu(),
    )


# =========================================================
# ADMIN CALLBACKS
# =========================================================

async def admin_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    if not is_admin(update):

        await safe_edit_message(
            query,
            "❌ У вас больше нет доступа.",
        )

        return

    data = query.data

    # -----------------------------------------------------
    # GROUPS
    # -----------------------------------------------------

    if data == "groups":

        groups = get_all_groups()

        if not groups:

            await safe_edit_message(
                query,
                "📭 Групп пока нет.",
                admin_menu(),
            )

            return

        await safe_edit_message(
            query,
            "👥 <b>Группы</b>\n\n"
            "Выберите группу:",
            groups_menu(),
        )

        return

    # -----------------------------------------------------
    # GROUP
    # -----------------------------------------------------

    if data.startswith("group:"):

        chat_id = int(
            data.split(":", 1)[1]
        )

        await safe_edit_message(
            query,
            make_group_overview(chat_id),
            group_menu(chat_id),
        )

        return

    # -----------------------------------------------------
    # STATS
    # -----------------------------------------------------

    if data.startswith("stats:"):

        chat_id = int(
            data.split(":", 1)[1]
        )

        await safe_edit_message(
            query,
            make_stats_text(chat_id),
            group_menu(chat_id),
        )

        return

    # -----------------------------------------------------
    # SETTINGS
    # -----------------------------------------------------

    if data.startswith("settings:"):

        chat_id = int(
            data.split(":", 1)[1]
        )

        settings_row = get_group_settings(chat_id)

        text = (
            "⚙️ <b>Настройки группы</b>\n\n"
            f"Название: <b>{settings_row['title']}</b>\n"
            f"Статус: "
            f"{'🟢 Включена' if settings_row['enabled'] else '🔴 Выключена'}\n"
            f"Hold: <b>{settings_row['hold_minutes']} мин.</b>"
        )

        await safe_edit_message(
            query,
            text,
            group_menu(chat_id),
        )

        return

    # -----------------------------------------------------
    # ENABLE
    # -----------------------------------------------------

    if data.startswith("enable:"):

        chat_id = int(
            data.split(":", 1)[1]
        )

        conn = get_db()

        try:
            cursor = conn.cursor()

            cursor.execute(
                """
                UPDATE groups
                SET enabled = 1
                WHERE chat_id = %s
                """,
                (chat_id,),
            )

            conn.commit()

        finally:
            conn.close()

        await safe_edit_message(
            query,
            make_group_overview(chat_id),
            group_menu(chat_id),
        )

        return

    # -----------------------------------------------------
    # DISABLE
    # -----------------------------------------------------

    if data.startswith("disable:"):

        chat_id = int(
            data.split(":", 1)[1]
        )

        try:

            await context.bot.send_message(
                chat_id=chat_id,
                text=TECHNICAL_BREAK_TEXT,
                parse_mode="HTML",
            )

        except Exception as error:

            logging.warning(
                "Не удалось отправить сообщение "
                "о техническом перерыве: %s",
                error,
            )

        conn = get_db()

        try:

            cursor = conn.cursor()

            cursor.execute(
                """
                UPDATE groups
                SET enabled = 0
                WHERE chat_id = %s
                """,
                (chat_id,),
            )

            conn.commit()

        finally:
            conn.close()

        await safe_edit_message(
            query,
            make_group_overview(chat_id),
            group_menu(chat_id),
        )

        return

    # -----------------------------------------------------
    # HOLD MENU
    # -----------------------------------------------------

    if data.startswith("hold:"):

        chat_id = int(
            data.split(":", 1)[1]
        )

        settings_row = get_group_settings(chat_id)

        text = (
            "⏱ <b>Настройка Hold</b>\n\n"
            f"Текущий hold: "
            f"<b>{settings_row['hold_minutes']} мин.</b>\n\n"
            "Выберите новое значение:"
        )

        keyboard = [
            [
                InlineKeyboardButton(
                    "1 мин",
                    callback_data=f"sethold:{chat_id}:1",
                ),
                InlineKeyboardButton(
                    "5 мин",
                    callback_data=f"sethold:{chat_id}:5",
                ),
            ],
            [
                InlineKeyboardButton(
                    "10 мин",
                    callback_data=f"sethold:{chat_id}:10",
                ),
                InlineKeyboardButton(
                    "30 мин",
                    callback_data=f"sethold:{chat_id}:30",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Назад",
                    callback_data=f"group:{chat_id}",
                )
            ],
        ]

        await safe_edit_message(
            query,
            text,
            InlineKeyboardMarkup(keyboard),
        )

        return

    # -----------------------------------------------------
    # SET HOLD
    # -----------------------------------------------------

    if data.startswith("sethold:"):

        _, chat_id_text, minutes_text = data.split(
            ":",
            2,
        )

        chat_id = int(chat_id_text)
        minutes = int(minutes_text)

        conn = get_db()

        try:

            cursor = conn.cursor()

            cursor.execute(
                """
                UPDATE groups
                SET hold_minutes = %s
                WHERE chat_id = %s
                """,
                (
                    minutes,
                    chat_id,
                ),
            )

            conn.commit()

        finally:
            conn.close()

        await safe_edit_message(
            query,
            make_group_overview(chat_id),
            group_menu(chat_id),
        )

        return

    # -----------------------------------------------------
    # CLEAR
    # -----------------------------------------------------

    if data.startswith("clear:"):

        chat_id = int(
            data.split(":", 1)[1]
        )

        keyboard = [
            [
                InlineKeyboardButton(
                    "✅ Да, очистить",
                    callback_data=f"confirm_clear:{chat_id}",
                ),
                InlineKeyboardButton(
                    "❌ Отмена",
                    callback_data=f"group:{chat_id}",
                ),
            ]
        ]

        await safe_edit_message(
            query,
            "⚠️ <b>Очистить статистику?</b>\n\n"
            "Все сохранённые QR-события этой группы "
            "будут удалены.",
            InlineKeyboardMarkup(keyboard),
        )

        return

    # -----------------------------------------------------
    # CONFIRM CLEAR
    # -----------------------------------------------------

    if data.startswith("confirm_clear:"):

        chat_id = int(
            data.split(":", 1)[1]
        )

        conn = get_db()

        try:

            cursor = conn.cursor()

            cursor.execute(
                """
                DELETE FROM qr_messages
                WHERE chat_id = %s
                """,
                (chat_id,),
            )

            conn.commit()

        finally:
            conn.close()

        await safe_edit_message(
            query,
            "✅ <b>Статистика очищена.</b>\n\n"
            "Все QR-события этой группы удалены.",
            group_menu(chat_id),
        )

        return

    # -----------------------------------------------------
    # ADMINS
    # -----------------------------------------------------

    if data == "admins":

        await safe_edit_message(
            query,
            "👑 <b>Администраторы</b>\n\n"
            "Выберите действие:",
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "👥 Список",
                            callback_data="admins_list",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "➕ Добавить",
                            callback_data="add_admin",
                        ),
                        InlineKeyboardButton(
                            "➖ Удалить",
                            callback_data="remove_admin",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ Назад",
                            callback_data="groups",
                        )
                    ],
                ]
            ),
        )

        return

    # -----------------------------------------------------
    # ADMIN LIST
    # -----------------------------------------------------

    if data == "admins_list":

        conn = get_db()

        try:

            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT username
                FROM admins
                ORDER BY username
                """
            )

            admins = cursor.fetchall()

        finally:
            conn.close()

        if admins:

            text = "👑 <b>Администраторы</b>\n\n"

            for admin in admins:

                text += (
                    f"• @{admin['username']}\n"
                )

        else:

            text = (
                "👑 <b>Администраторы</b>\n\n"
                "Список пуст."
            )

        keyboard = [
            [
                InlineKeyboardButton(
                    "⬅️ Назад",
                    callback_data="admins",
                )
            ]
        ]

        await safe_edit_message(
            query,
            text,
            InlineKeyboardMarkup(keyboard),
        )

        return

    # -----------------------------------------------------
    # ADD ADMIN
    # -----------------------------------------------------

    if data == "add_admin":

        context.user_data["admin_action"] = "add"
        context.user_data["waiting_for_admin"] = True

        await safe_edit_message(
            query,
            "➕ <b>Добавление администратора</b>\n\n"
            "Отправьте username пользователя "
            "в формате:\n\n"
            "<code>@username</code>",
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Назад",
                            callback_data="admins",
                        )
                    ]
                ]
            ),
        )

        return

    # -----------------------------------------------------
    # REMOVE ADMIN
    # -----------------------------------------------------

    if data == "remove_admin":

        conn = get_db()

        try:

            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT username
                FROM admins
                ORDER BY username
                """
            )

            admins = cursor.fetchall()

        finally:
            conn.close()

        if not admins:

            await safe_edit_message(
                query,
                "❌ Администраторов нет.",
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "⬅️ Назад",
                                callback_data="admins",
                            )
                        ]
                    ]
                ),
            )

            return

        keyboard = []

        for admin in admins:

            username = admin["username"]

            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"❌ @{username}",
                        callback_data=f"remove:{username}",
                    )
                ]
            )

        keyboard.append(
            [
                InlineKeyboardButton(
                    "⬅️ Назад",
                    callback_data="admins",
                )
            ]
        )

        await safe_edit_message(
            query,
            "➖ <b>Удаление администратора</b>\n\n"
            "Выберите администратора:",
            InlineKeyboardMarkup(keyboard),
        )

        return

    # -----------------------------------------------------
    # REMOVE ADMIN
    # -----------------------------------------------------

    if data.startswith("remove:"):

        username = data.split(":", 1)[1]

        conn = get_db()

        try:

            cursor = conn.cursor()

            cursor.execute(
                """
                DELETE FROM admins
                WHERE username = %s
                """,
                (username,),
            )

            conn.commit()

        finally:
            conn.close()

        await safe_edit_message(
            query,
            f"✅ Администратор @{username} удалён.",
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ К администраторам",
                            callback_data="admins",
                        )
                    ]
                ]
            ),
        )

        return

    # -----------------------------------------------------
    # ADMIN
    # -----------------------------------------------------

    if data == "admin":

        await safe_edit_message(
            query,
            "⚙️ <b>Панель администратора</b>\n\n"
            "Выберите раздел:",
            admin_menu(),
        )

        return


# =========================================================
# ADMIN INPUT
# =========================================================

async def handle_admin_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    if update.effective_chat.type != "private":
        return

    if not is_admin(update):
        return

    text = update.message.text.strip()

    # -----------------------------------------------------
    # HOLD
    # -----------------------------------------------------

    if "waiting_hold" in context.user_data:

        chat_id = context.user_data["waiting_hold"]

        try:
            minutes = int(text)

        except ValueError:

            await update.message.reply_text(
                "❌ Введите целое число минут."
            )

            return

        if minutes < 0 or minutes > 1440:

            await update.message.reply_text(
                "❌ Hold должен быть от 0 до 1440 минут."
            )

            return

        conn = get_db()

        try:

            cursor = conn.cursor()

            cursor.execute(
                """
                UPDATE groups
                SET hold_minutes = %s
                WHERE chat_id = %s
                """,
                (
                    minutes,
                    chat_id,
                ),
            )

            conn.commit()

        finally:
            conn.close()

        del context.user_data["waiting_hold"]

        await update.message.reply_text(
            f"✅ Hold установлен: "
            f"<b>{minutes} минут</b>.",
            parse_mode="HTML",
            reply_markup=group_menu(chat_id),
        )

        return

    # -----------------------------------------------------
    # ADD ADMIN
    # -----------------------------------------------------

    if context.user_data.get("waiting_for_admin"):

        username = normalize_username(text)

        if not username:

            await update.message.reply_text(
                "❌ Неверный username."
            )

            return

        owner = normalize_username(
            OWNER_USERNAME
        )

        if username == owner:

            await update.message.reply_text(
                "ℹ️ Это главный администратор."
            )

            context.user_data[
                "waiting_for_admin"
            ] = False

            return

        conn = get_db()

        try:

            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT user_id
                FROM user_cache
                WHERE username = %s
                """,
                (username,),
            )

            row = cursor.fetchone()

        finally:
            conn.close()

        if row is None:

            await update.message.reply_text(
                "❌ Я не знаю Telegram ID этого пользователя.\n\n"
                f"Пусть @{username} сначала отправит "
                "этому боту /start.\n\n"
                "После этого добавьте его ещё раз."
            )

            context.user_data[
                "waiting_for_admin"
            ] = False

            return

        add_admin(
            row["user_id"],
            username,
        )

        context.user_data[
            "waiting_for_admin"
        ] = False

        await update.message.reply_text(
            f"✅ <b>@{username}</b> добавлен "
            "в администраторы.",
            parse_mode="HTML",
            reply_markup=admins_menu(),
        )


# =========================================================
# QR DETECTION
# =========================================================

def contains_qr(image_bytes):

    try:

        image_array = np.frombuffer(
            image_bytes,
            dtype=np.uint8,
        )

        image = cv2.imdecode(
            image_array,
            cv2.IMREAD_COLOR,
        )

        if image is None:

            logging.warning(
                "Не удалось открыть изображение"
            )

            return False

        detector = cv2.QRCodeDetector()

        # 1. Original image

        try:

            ok, points = detector.detect(image)

            if ok and points is not None:

                logging.info(
                    "QR-код обнаружен"
                )

                return True

        except Exception as error:

            logging.warning(
                "Ошибка detect(): %s",
                error,
            )

        # 2. Enlarged

        height, width = image.shape[:2]

        scale = 2

        enlarged = cv2.resize(
            image,
            (
                width * scale,
                height * scale,
            ),
            interpolation=cv2.INTER_CUBIC,
        )

        try:

            ok, points = detector.detect(
                enlarged
            )

            if ok and points is not None:

                logging.info(
                    "QR-код обнаружен после увеличения"
                )

                return True

        except Exception as error:

            logging.warning(
                "Ошибка detect() после увеличения: %s",
                error,
            )

        # 3. Grayscale

        gray = cv2.cvtColor(
            enlarged,
            cv2.COLOR_BGR2GRAY,
        )

        try:

            ok, points = detector.detect(
                gray
            )

            if ok and points is not None:

                logging.info(
                    "QR-код обнаружен на grayscale"
                )

                return True

        except Exception as error:

            logging.warning(
                "Ошибка detect() grayscale: %s",
                error,
            )

        logging.info(
            "QR-код не обнаружен"
        )

        return False

    except Exception as error:

        logging.exception(
            "Ошибка проверки изображения на QR: %s",
            error,
        )

        return False


# =========================================================
# PHOTO / QR
# =========================================================

async def handle_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.message

    if not message:
        return

    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user:
        return

    if chat.type not in ("group", "supergroup"):
        return

    settings_row = get_group_settings(
        chat.id
    )

    if not settings_row["enabled"]:
        return

    try:

        photo = message.photo[-1]

        telegram_file = await context.bot.get_file(
            photo.file_id
        )

        image_bytes = (
            await telegram_file.download_as_bytearray()
        )

    except Exception as error:

        logging.warning(
            "Не удалось скачать изображение: %s",
            error,
        )

        return

    # Только QR
    if not contains_qr(
        bytes(image_bytes)
    ):

        logging.info(
            "Обычная фотография, не QR: %s (%s)",
            user.full_name,
            user.id,
        )

        return

    hold_minutes = settings_row[
        "hold_minutes"
    ]

    now = datetime.now(timezone.utc)

    conn = get_db()

    try:

        cursor = conn.cursor()

        # Последний засчитанный QR именно
        # этого пользователя в этой группе.
        cursor.execute(
            """
            SELECT sent_at
            FROM qr_messages
            WHERE chat_id = %s
              AND user_id = %s
              AND counted = 1
            ORDER BY id DESC
            LIMIT 1
            """,
            (
                chat.id,
                user.id,
            ),
        )

        last_counted = cursor.fetchone()

        counted = False

        if last_counted is None:

            counted = True

        else:

            last_time = last_counted[
                "sent_at"
            ]

            # PostgreSQL возвращает datetime
            # с timezone.
            if last_time.tzinfo is None:

                last_time = last_time.replace(
                    tzinfo=timezone.utc
                )

            difference = (
                now - last_time
            ).total_seconds()

            if difference >= hold_minutes * 60:

                counted = True

        username = (
            user.username or ""
        )

        display_name = (
            user.full_name
            or username
            or str(user.id)
        )

        cursor.execute(
            """
            INSERT INTO qr_messages (
                chat_id,
                user_id,
                username,
                display_name,
                sent_at,
                counted
            )
            VALUES (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s
            )
            """,
            (
                chat.id,
                user.id,
                username,
                display_name,
                now,
                1 if counted else 0,
            ),
        )

        conn.commit()

    except Exception:

        conn.rollback()

        logging.exception(
            "Ошибка сохранения QR в PostgreSQL"
        )

    finally:

        conn.close()

    logging.info(
        "%s QR: %s (%s)",
        "COUNTED"
        if counted
        else "IGNORED/HOLD",
        display_name,
        user.id,
    )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    logging.error(
        "Exception while handling update:",
        exc_info=context.error,
    )


# =========================================================
# /ON
# =========================================================

async def enable_bot(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    register_owner(update)

    if not is_admin(update):

        await update.message.reply_text(
            "❌ У вас нет прав администратора бота."
        )

        return

    chat = update.effective_chat

    if chat.type not in (
        "group",
        "supergroup",
    ):

        await update.message.reply_text(
            "❌ Эту команду нужно использовать в группе."
        )

        return

    ensure_group(
        chat.id,
        chat.title,
    )

    conn = get_db()

    try:

        cursor = conn.cursor()

        cursor.execute(
            """
            UPDATE groups
            SET enabled = 1
            WHERE chat_id = %s
            """,
            (chat.id,),
        )

        conn.commit()

    finally:
        conn.close()

    await update.message.reply_text(
        "🟢 Сканирование включено."
    )


# =========================================================
# /OFF
# =========================================================

async def disable_bot(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    register_owner(update)

    if not is_admin(update):

        await update.message.reply_text(
            "❌ У вас нет прав администратора бота."
        )

        return

    chat = update.effective_chat

    if chat.type not in (
        "group",
        "supergroup",
    ):

        await update.message.reply_text(
            "❌ Эту команду нужно использовать в группе."
        )

        return

    ensure_group(
        chat.id,
        chat.title,
    )

    await context.bot.send_message(
        chat_id=chat.id,
        text=TECHNICAL_BREAK_TEXT,
        parse_mode="HTML",
    )

    conn = get_db()

    try:

        cursor = conn.cursor()

        cursor.execute(
            """
            UPDATE groups
            SET enabled = 0
            WHERE chat_id = %s
            """,
            (chat.id,),
        )

        conn.commit()

    finally:
        conn.close()


# =========================================================
# /SETHOLD
# =========================================================

async def set_hold(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    register_owner(update)

    if not is_admin(update):

        await update.message.reply_text(
            "❌ У вас нет прав администратора бота."
        )

        return

    chat = update.effective_chat

    if chat.type not in (
        "group",
        "supergroup",
    ):

        await update.message.reply_text(
            "❌ Эту команду нужно использовать в группе."
        )

        return

    if not context.args:

        await update.message.reply_text(
            "Использование:\n/sethold 10"
        )

        return

    try:

        minutes = int(
            context.args[0]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ Укажи целое число минут."
        )

        return

    if minutes < 0 or minutes > 1440:

        await update.message.reply_text(
            "❌ Hold должен быть от 0 до 1440 минут."
        )

        return

    ensure_group(
        chat.id,
        chat.title,
    )

    conn = get_db()

    try:

        cursor = conn.cursor()

        cursor.execute(
            """
            UPDATE groups
            SET hold_minutes = %s
            WHERE chat_id = %s
            """,
            (
                minutes,
                chat.id,
            ),
        )

        conn.commit()

    finally:
        conn.close()

    await update.message.reply_text(
        f"⏱ Hold установлен: {minutes} минут."
    )


# =========================================================
# /SETTINGS
# =========================================================

async def settings(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    register_owner(update)

    if not is_admin(update):

        await update.message.reply_text(
            "❌ У вас нет прав администратора бота."
        )

        return

    chat = update.effective_chat

    if chat.type not in (
        "group",
        "supergroup",
    ):

        await update.message.reply_text(
            "❌ Эту команду нужно использовать в группе."
        )

        return

    ensure_group(
        chat.id,
        chat.title,
    )

    row = get_group_settings(
        chat.id
    )

    status = (
        "🟢 Включено"
        if row["enabled"]
        else "🔴 Выключено"
    )

    await update.message.reply_text(
        "⚙️ Настройки\n\n"
        f"Статус: {status}\n"
        f"⏱ Hold: {row['hold_minutes']} минут"
    )


# =========================================================
# /STATS
# =========================================================

async def stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat = update.effective_chat

    if chat.type not in (
        "group",
        "supergroup",
    ):

        await update.message.reply_text(
            "❌ Эту команду нужно использовать в группе."
        )

        return

    ensure_group(
        chat.id,
        chat.title,
    )

    await update.message.reply_text(
        make_stats_text(chat.id),
        parse_mode="HTML",
    )


# =========================================================
# /CLEAR
# =========================================================

async def clear_stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    register_owner(update)

    if not is_admin(update):

        await update.message.reply_text(
            "❌ У вас нет прав администратора бота."
        )

        return

    chat = update.effective_chat

    if chat.type not in (
        "group",
        "supergroup",
    ):

        await update.message.reply_text(
            "❌ Эту команду нужно использовать в группе."
        )

        return

    conn = get_db()

    try:

        cursor = conn.cursor()

        cursor.execute(
            """
            DELETE FROM qr_messages
            WHERE chat_id = %s
            """,
            (chat.id,),
        )

        conn.commit()

    finally:
        conn.close()

    await update.message.reply_text(
        "🗑 Статистика очищена."
    )


# =========================================================
# MAIN
# =========================================================

def main():

    # Инициализация PostgreSQL
    init_db()

    # Render health server
    threading.Thread(
        target=start_render_health_server,
        daemon=True,
    ).start()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # =====================================================
    # COMMANDS
    # =====================================================

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "on",
            enable_bot,
        )
    )

    application.add_handler(
        CommandHandler(
            "off",
            disable_bot,
        )
    )

    application.add_handler(
        CommandHandler(
            "sethold",
            set_hold,
        )
    )

    application.add_handler(
        CommandHandler(
            "settings",
            settings,
        )
    )

    application.add_handler(
        CommandHandler(
            "stats",
            stats,
        )
    )

    application.add_handler(
        CommandHandler(
            "clear",
            clear_stats,
        )
    )

    # =====================================================
    # ADMIN CALLBACKS
    # =====================================================

    application.add_handler(
        CallbackQueryHandler(
            admin_panel
        )
    )

    # =====================================================
    # REGISTER GROUP
    # =====================================================

    application.add_handler(
        MessageHandler(
            filters.ALL,
            register_group,
        ),
        group=5,
    )

    # =====================================================
    # CACHE USERS
    # =====================================================

    application.add_handler(
        MessageHandler(
            filters.ALL,
            cache_user,
        ),
        group=10,
    )

    # =====================================================
    # PRIVATE ADMIN INPUT
    # =====================================================

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_admin_input,
        ),
        group=1,
    )

    # =====================================================
    # QR / PHOTO
    # =====================================================

    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            handle_photo,
        ),
        group=0,
    )

    # =====================================================
    # ERROR
    # =====================================================

    application.add_error_handler(
        error_handler
    )

    print("Bot started...")

    # =====================================================
    # START BOT
    # =====================================================

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()

