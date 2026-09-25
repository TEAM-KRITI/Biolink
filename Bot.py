# ============================================================
# BIOGUARD / BIOMUTEBOT
# Telegram Bio + Link Protection Bot
# Python 3.11+ / python-telegram-bot 22.x
# ============================================================

import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from telegram import (
    Update,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()

UPDATE_CHANNEL = os.getenv(
    "UPDATE_CHANNEL",
    "https://t.me/annu_support",
).strip()

SUPPORT_CHANNEL = os.getenv(
    "SUPPORT_CHANNEL",
    "https://t.me/annu_updates",
).strip()

# MongoDB is optional. If MONGO_URI is not configured,
# the bot automatically uses SQLite so Heroku does not crash.
MONGO_URI = os.getenv("MONGO_URI", "").strip()
MONGO_DB = os.getenv("MONGO_DB", "bioguard").strip()

# Global mute duration after 3 warnings.
DEFAULT_MUTE_HOURS = int(os.getenv("MUTE_HOURS", "2"))

# Warning/notice messages are deleted after 5 minutes.
NOTICE_DELETE_SECONDS = int(
    os.getenv("NOTICE_DELETE_SECONDS", "300")
)

# ============================================================
# VALIDATION
# ============================================================

if not BOT_TOKEN:
    raise ValueError(
        "BOT_TOKEN is missing. Add BOT_TOKEN in Heroku Config Vars."
    )

if not OWNER_ID_RAW:
    raise ValueError(
        "OWNER_ID is missing. Add OWNER_ID in Heroku Config Vars."
    )

try:
    OWNER_ID = int(OWNER_ID_RAW)
except ValueError:
    raise ValueError("OWNER_ID must be a numeric Telegram user ID.")

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("BioGuard")

# ============================================================
# OPTIONAL MONGODB
# ============================================================

mongo = None
mongo_db = None
users_collection = None
groups_collection = None
warnings_collection = None
settings_collection = None
free_users_collection = None

if MONGO_URI:
    try:
        from pymongo import MongoClient

        mongo = MongoClient(
            MONGO_URI,
            serverSelectionTimeoutMS=8000,
            connectTimeoutMS=8000,
            socketTimeoutMS=8000,
        )
        mongo.admin.command("ping")
        mongo_db = mongo[MONGO_DB]

        users_collection = mongo_db["users"]
        groups_collection = mongo_db["groups"]
        warnings_collection = mongo_db["warnings"]
        settings_collection = mongo_db["settings"]
        free_users_collection = mongo_db["free_users"]

        settings_collection.update_one(
            {"_id": "global"},
            {"$setOnInsert": {"mute_duration": DEFAULT_MUTE_HOURS}},
            upsert=True,
        )

        logger.info("MongoDB connected successfully.")

    except Exception as e:
        logger.warning(
            "MongoDB connection failed: %s. Falling back to SQLite.",
            e,
        )
        mongo = None
        mongo_db = None
        users_collection = None
        groups_collection = None
        warnings_collection = None
        settings_collection = None
        free_users_collection = None
else:
    logger.warning(
        "MONGO_URI is not configured. Using SQLite fallback."
    )

# ============================================================
# SQLITE FALLBACK
# ============================================================

DB_PATH = os.getenv("DB_PATH", "bioguard.db")
sqlite_db = sqlite3.connect(
    DB_PATH,
    check_same_thread=False,
)
sqlite_db.row_factory = sqlite3.Row

sqlite_db.executescript(
    """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        first_name TEXT DEFAULT '',
        last_name TEXT DEFAULT '',
        username TEXT DEFAULT '',
        updated_at TEXT DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS groups_table (
        id INTEGER PRIMARY KEY,
        title TEXT DEFAULT '',
        username TEXT DEFAULT '',
        updated_at TEXT DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS warnings (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT DEFAULT '',
        PRIMARY KEY (chat_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS free_users (
        user_id INTEGER PRIMARY KEY,
        first_name TEXT DEFAULT '',
        username TEXT DEFAULT '',
        added_at TEXT DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS approved_users (
        user_id INTEGER PRIMARY KEY,
        approved_at TEXT DEFAULT ''
    );
    """
)
sqlite_db.commit()

sqlite_db.execute(
    "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
    ("mute_duration", str(DEFAULT_MUTE_HOURS)),
)
sqlite_db.commit()

# ============================================================
# HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def iso_now():
    return now_utc().isoformat()


def normalize_channel_url(value: str) -> str:
    value = (value or "").strip()

    if not value:
        return ""

    if value.startswith("@"):
        return f"https://t.me/{value[1:]}"

    if value.startswith("https://") or value.startswith("http://"):
        return value

    if value.startswith("t.me/"):
        return f"https://{value}"

    return f"https://t.me/{value}"


def channel_username(value: str) -> str:
    value = (value or "").strip()

    if value.startswith("@"):
        return value[1:]

    value = re.sub(r"^https?://t\.me/", "", value)
    value = value.strip("/")

    return value


def display_name(user) -> str:
    name = " ".join(
        x for x in [user.first_name, user.last_name]
        if x
    ).strip()

    return name or "User"


def mention_html(user) -> str:
    return (
        f'<a href="tg://user?id={user.id}">'
        f"{escape(display_name(user))}"
        f"</a>"
    )


# ============================================================
# DATABASE FUNCTIONS
# ============================================================

def save_user(user):
    if not user:
        return

    if users_collection is not None:
        users_collection.update_one(
            {"_id": user.id},
            {
                "$set": {
                    "id": user.id,
                    "first_name": user.first_name or "",
                    "last_name": user.last_name or "",
                    "username": user.username or "",
                    "updated_at": now_utc(),
                }
            },
            upsert=True,
        )
        return

    sqlite_db.execute(
        """
        INSERT INTO users(id, first_name, last_name, username, updated_at)
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            username=excluded.username,
            updated_at=excluded.updated_at
        """,
        (
            user.id,
            user.first_name or "",
            user.last_name or "",
            user.username or "",
            iso_now(),
        ),
    )
    sqlite_db.commit()


def save_group(chat):
    if not chat:
        return

    if groups_collection is not None:
        groups_collection.update_one(
            {"_id": chat.id},
            {
                "$set": {
                    "id": chat.id,
                    "title": chat.title or "",
                    "username": chat.username or "",
                    "updated_at": now_utc(),
                }
            },
            upsert=True,
        )
        return

    sqlite_db.execute(
        """
        INSERT INTO groups_table(id, title, username, updated_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title,
            username=excluded.username,
            updated_at=excluded.updated_at
        """,
        (
            chat.id,
            chat.title or "",
            chat.username or "",
            iso_now(),
        ),
    )
    sqlite_db.commit()


def get_mute_duration():
    if settings_collection is not None:
        data = settings_collection.find_one({"_id": "global"})
        if data:
            try:
                return max(1, int(data.get("mute_duration", DEFAULT_MUTE_HOURS)))
            except Exception:
                pass
        return DEFAULT_MUTE_HOURS

    row = sqlite_db.execute(
        "SELECT value FROM settings WHERE key='mute_duration'"
    ).fetchone()

    try:
        return max(1, int(row["value"]))
    except Exception:
        return DEFAULT_MUTE_HOURS


def set_mute_duration(hours):
    hours = max(1, int(hours))

    if settings_collection is not None:
        settings_collection.update_one(
            {"_id": "global"},
            {"$set": {"mute_duration": hours}},
            upsert=True,
        )
        return

    sqlite_db.execute(
        """
        INSERT INTO settings(key, value)
        VALUES('mute_duration', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (str(hours),),
    )
    sqlite_db.commit()


def get_warning(chat_id, user_id):
    if warnings_collection is not None:
        data = warnings_collection.find_one(
            {"chat_id": chat_id, "user_id": user_id}
        )
        return int(data.get("count", 0)) if data else 0

    row = sqlite_db.execute(
        """
        SELECT count FROM warnings
        WHERE chat_id=? AND user_id=?
        """,
        (chat_id, user_id),
    ).fetchone()

    return int(row["count"]) if row else 0


def add_warning(chat_id, user_id):
    old = get_warning(chat_id, user_id)
    new_count = old + 1

    if warnings_collection is not None:
        warnings_collection.update_one(
            {"chat_id": chat_id, "user_id": user_id},
            {
                "$set": {
                    "count": new_count,
                    "updated_at": now_utc(),
                }
            },
            upsert=True,
        )
        return new_count

    sqlite_db.execute(
        """
        INSERT INTO warnings(chat_id, user_id, count, updated_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET
            count=excluded.count,
            updated_at=excluded.updated_at
        """,
        (chat_id, user_id, new_count, iso_now()),
    )
    sqlite_db.commit()

    return new_count


def reset_warning(chat_id, user_id):
    if warnings_collection is not None:
        warnings_collection.delete_one(
            {"chat_id": chat_id, "user_id": user_id}
        )
        return

    sqlite_db.execute(
        "DELETE FROM warnings WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    )
    sqlite_db.commit()


def is_free_user(user_id):
    if free_users_collection is not None:
        return bool(
            free_users_collection.find_one({"_id": user_id})
        )

    row = sqlite_db.execute(
        "SELECT user_id FROM free_users WHERE user_id=?",
        (user_id,),
    ).fetchone()

    return row is not None


def add_free_user(user):
    if free_users_collection is not None:
        free_users_collection.update_one(
            {"_id": user.id},
            {
                "$set": {
                    "user_id": user.id,
                    "first_name": user.first_name or "",
                    "username": user.username or "",
                    "added_at": now_utc(),
                }
            },
            upsert=True,
        )
        return

    sqlite_db.execute(
        """
        INSERT INTO free_users(user_id, first_name, username, added_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            first_name=excluded.first_name,
            username=excluded.username,
            added_at=excluded.added_at
        """,
        (
            user.id,
            user.first_name or "",
            user.username or "",
            iso_now(),
        ),
    )
    sqlite_db.commit()


def remove_free_user(user_id):
    if free_users_collection is not None:
        free_users_collection.delete_one({"_id": user_id})
        return

    sqlite_db.execute(
        "DELETE FROM free_users WHERE user_id=?",
        (user_id,),
    )
    sqlite_db.commit()


def is_approved_user(user_id):
    if mongo_db is not None:
        return bool(
            mongo_db["approved_users"].find_one({"_id": user_id})
        )

    row = sqlite_db.execute(
        "SELECT user_id FROM approved_users WHERE user_id=?",
        (user_id,),
    ).fetchone()

    return row is not None


def add_approved_user(user_id):
    if mongo_db is not None:
        mongo_db["approved_users"].update_one(
            {"_id": user_id},
            {"$set": {"approved_at": now_utc()}},
            upsert=True,
        )
        return

    sqlite_db.execute(
        """
        INSERT OR REPLACE INTO approved_users(user_id, approved_at)
        VALUES(?, ?)
        """,
        (user_id, iso_now()),
    )
    sqlite_db.commit()


def remove_approved_user(user_id):
    if mongo_db is not None:
        mongo_db["approved_users"].delete_one({"_id": user_id})
        return

    sqlite_db.execute(
        "DELETE FROM approved_users WHERE user_id=?",
        (user_id,),
    )
    sqlite_db.commit()


def total_users():
    if users_collection is not None:
        return users_collection.count_documents({})
    return sqlite_db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]


def total_groups():
    if groups_collection is not None:
        return groups_collection.count_documents({})
    return sqlite_db.execute(
        "SELECT COUNT(*) c FROM groups_table"
    ).fetchone()["c"]


def iter_user_ids():
    if users_collection is not None:
        return [x["_id"] for x in users_collection.find({}, {"_id": 1})]

    rows = sqlite_db.execute("SELECT id FROM users").fetchall()
    return [int(x["id"]) for x in rows]


def iter_group_ids():
    if groups_collection is not None:
        return [x["_id"] for x in groups_collection.find({}, {"_id": 1})]

    rows = sqlite_db.execute(
        "SELECT id FROM groups_table"
    ).fetchall()
    return [int(x["id"]) for x in rows]


# ============================================================
# LINK / USERNAME DETECTION
# ============================================================

LINK_REGEX = re.compile(
    r"""(?ix)
    (
        https?://[^\s<>"']+
        |www\.[^\s<>"']+
        |(?:t\.me|telegram\.me|telegram\.dog)/[^\s<>"']+
        |(?:instagram\.com|facebook\.com|twitter\.com|x\.com)/
            [^\s<>"']*
        |(?:youtube\.com|youtu\.be)/[^\s<>"']*
        |(?:wa\.me|whatsapp\.com)/[^\s<>"']*
        |(?:discord\.gg|discord\.com/invite)/[^\s<>"']*
    )
    """
)

USERNAME_REGEX = re.compile(
    r"(?<![\w])@[A-Za-z0-9_]{4,32}(?![\w])"
)

# Common Telegram link-looking formats without @.
TELEGRAM_TEXT_REGEX = re.compile(
    r"(?ix)\b(?:t\.me|telegram\.me|telegram\.dog)\b"
)


def has_link(text):
    if not text:
        return False

    return bool(
        LINK_REGEX.search(text)
        or TELEGRAM_TEXT_REGEX.search(text)
    )


def has_username(text):
    if not text:
        return False

    return bool(USERNAME_REGEX.search(text))


def has_forbidden_content(text):
    if not text:
        return False

    return has_link(text) or has_username(text)


# ============================================================
# BOT INFO
# ============================================================

async def get_bot_username(context):
    try:
        me = await context.bot.get_me()
        return me.username or ""
    except Exception:
        return ""


# ============================================================
# BIO CHECK
# ============================================================

async def get_user_bio(context, user_id):
    """
    Telegram Bot API does not expose a separate 'get user bio'
    method. get_chat(user_id) can expose the private chat bio when
    Telegram makes that chat/user information available to the bot.
    """
    try:
        chat = await context.bot.get_chat(user_id)
        return chat.bio or ""
    except TelegramError as e:
        logger.debug(
            "Could not read bio for %s: %s",
            user_id,
            e,
        )
        return ""
    except Exception:
        return ""


# ============================================================
# SUBSCRIPTION
# ============================================================

async def is_subscribed(context, user_id):
    if not UPDATE_CHANNEL:
        return True

    target = UPDATE_CHANNEL

    try:
        member = await context.bot.get_chat_member(
            target,
            user_id,
        )

        return member.status in (
            "member",
            "administrator",
            "creator",
        )

    except Exception:
        # Do not block the bot if the channel username/config is wrong.
        logger.warning(
            "Could not verify update-channel membership for user %s.",
            user_id,
        )
        return True


# ============================================================
# ADMIN CHECK
# ============================================================

async def is_admin(context, chat_id, user_id):
    try:
        member = await context.bot.get_chat_member(
            chat_id,
            user_id,
        )

        return member.status in (
            "administrator",
            "creator",
        )

    except Exception:
        return False


# ============================================================
# KEYBOARDS
# ============================================================

def start_keyboard(bot_username):
    rows = []

    if bot_username:
        rows.append(
            [
                InlineKeyboardButton(
                    "➕ ᴀᴅᴅ ᴍᴇ ᴛᴏ ɢʀᴏᴜᴘ",
                    url=(
                        f"https://t.me/{bot_username}"
                        "?startgroup=true"
                    ),
                )
            ]
        )

    second = []

    update_url = normalize_channel_url(UPDATE_CHANNEL)
    support_url = normalize_channel_url(SUPPORT_CHANNEL)

    if update_url:
        second.append(
            InlineKeyboardButton(
                "🔄 ᴜᴘᴅᴀᴛᴇ",
                url=update_url,
            )
        )

    if support_url:
        second.append(
            InlineKeyboardButton(
                "💬 sᴜᴘᴘᴏʀᴛ",
                url=support_url,
            )
        )

    if second:
        rows.append(second)

    owner_url = (
        normalize_channel_url(OWNER_USERNAME)
        if OWNER_USERNAME
        else f"tg://user?id={OWNER_ID}"
    )

    rows.append(
        [
            InlineKeyboardButton(
                "👑 ᴏᴡɴᴇʀ",
                url=owner_url,
            ),
            InlineKeyboardButton(
                "❔ ʜᴇʟᴘ",
                callback_data="help",
            ),
        ]
    )

    return InlineKeyboardMarkup(rows)


def back_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔙 ʙᴀᴄᴋ",
                    callback_data="start",
                )
            ]
        ]
    )


def warning_keyboard():
    rows = []

    update_url = normalize_channel_url(UPDATE_CHANNEL)
    support_url = normalize_channel_url(SUPPORT_CHANNEL)

    buttons = []

    if update_url:
        buttons.append(
            InlineKeyboardButton(
                "🚀 ᴜᴘᴅᴀᴛᴇ",
                url=update_url,
            )
        )

    if support_url:
        buttons.append(
            InlineKeyboardButton(
                "💬 sᴜᴘᴘᴏʀᴛ",
                url=support_url,
            )
        )

    if buttons:
        rows.append(buttons)

    return InlineKeyboardMarkup(rows) if rows else None


def help_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔙 ʙᴀᴄᴋ",
                    callback_data="start",
                )
            ]
        ]
    )


# ============================================================
# SAFE DELETE
# ============================================================

async def safe_delete_message(context, chat_id, message_id):
    try:
        await context.bot.delete_message(
            chat_id=chat_id,
            message_id=message_id,
        )
        return True
    except Exception as e:
        logger.debug(
            "Message delete failed %s/%s: %s",
            chat_id,
            message_id,
            e,
        )
        return False


async def delete_later(context, chat_id, message_id, seconds):
    await asyncio.sleep(seconds)
    await safe_delete_message(
        context,
        chat_id,
        message_id,
    )


def schedule_delete(context, chat_id, message_id, seconds=NOTICE_DELETE_SECONDS):
    context.application.create_task(
        delete_later(
            context,
            chat_id,
            message_id,
            seconds,
        )
    )


# ============================================================
# MUTE
# ============================================================

async def mute_user(context, chat_id, user_id, hours=None):
    if hours is None:
        hours = get_mute_duration()

    until = now_utc() + timedelta(hours=hours)

    permissions = ChatPermissions(
        can_send_messages=False
    )

    await context.bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        permissions=permissions,
        until_date=until,
    )


async def permanent_mute(context, chat_id, user_id):
    permissions = ChatPermissions(
        can_send_messages=False
    )

    await context.bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        permissions=permissions,
    )


async def unmute_user(context, chat_id, user_id):
    permissions = ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )

    await context.bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        permissions=permissions,
    )


# ============================================================
# WARNING MESSAGE
# ============================================================

async def send_bio_warning(
    context,
    chat_id,
    user,
    warning_count,
    reason="your bio contains a link.",
):
    """
    This creates the warning style shown in the screenshot.
    The warning itself is automatically deleted after 5 minutes.
    """

    if warning_count >= 3:
        title = "🚨 <b>ʙɪᴏ ʟɪɴᴋ ᴘʀᴏᴛᴇᴄᴛɪᴏɴ</b>"
    else:
        title = "🚨 <b>ʙɪᴏ ʟɪɴᴋ ᴘʀᴏᴛᴇᴄᴛɪᴏɴ</b>"

    text = (
        f"{title}\n\n"
        f"🚨 {mention_html(user)}, your message was deleted "
        f"because {escape(reason)}\n\n"
        f"⚠️ <b>ᴡᴀʀɴɪɴɢ {warning_count}/3</b>\n\n"
        f"🔗 <b>ʙɪᴏ ʟɪɴᴋs ᴀʀᴇ ɴᴏᴛ ᴀʟʟᴏᴡᴇᴅ ɪɴ ᴛʜɪs ɢʀᴏᴜᴘ.</b>"
    )

    markup = warning_keyboard()

    try:
        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )

        # EXACTLY 5 MINUTES
        schedule_delete(
            context,
            chat_id,
            sent.message_id,
            NOTICE_DELETE_SECONDS,
        )

        return sent

    except Exception as e:
        logger.warning(
            "Warning message error: %s",
            e,
        )
        return None


async def send_mute_notice(
    context,
    chat_id,
    user,
    reason,
    duration_text,
):
    text = (
        "🔇 <b>ʙɪᴏɢᴜᴀʀᴅ ᴍᴜᴛᴇ</b>\n\n"
        f"👤 <b>ᴜsᴇʀ:</b> {mention_html(user)}\n"
        f"🆔 <b>ɪᴅ:</b> <code>{user.id}</code>\n\n"
        f"⛔ <b>ʀᴇᴀsᴏɴ:</b> {escape(reason)}\n"
        f"⏱ <b>ᴅᴜʀᴀᴛɪᴏɴ:</b> {escape(duration_text)}\n\n"
        "⚠️ <b>3/3 warnings reached.</b>"
    )

    markup = warning_keyboard()

    try:
        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )

        schedule_delete(
            context,
            chat_id,
            sent.message_id,
            NOTICE_DELETE_SECONDS,
        )

    except Exception as e:
        logger.warning(
            "Mute notice error: %s",
            e,
        )


# ============================================================
# MODERATION
# ============================================================

async def check_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat or not user:
        return

    if chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        return

    if user.is_bot:
        return

    save_user(user)
    save_group(chat)

    if user.id == OWNER_ID:
        return

    if is_free_user(user.id):
        return

    if is_approved_user(user.id):
        return

    # Admins are always exempt.
    if await is_admin(
        context,
        chat.id,
        user.id,
    ):
        return

    # --------------------------------------------------------
    # NAME CHECK
    # --------------------------------------------------------

    full_name = " ".join(
        x for x in [
            user.first_name,
            user.last_name,
        ]
        if x
    )

    if has_forbidden_content(full_name):
        try:
            await permanent_mute(
                context,
                chat.id,
                user.id,
            )
        except Exception as e:
            logger.warning(
                "Could not permanently mute bad-name user: %s",
                e,
            )

        await safe_delete_message(
            context,
            chat.id,
            message.message_id,
        )

        reset_warning(
            chat.id,
            user.id,
        )

        await send_mute_notice(
            context,
            chat.id,
            user,
            "Link or username detected in profile name.",
            "ᴘᴇʀᴍᴀɴᴇɴᴛ",
        )

        return

    # --------------------------------------------------------
    # MESSAGE CHECK
    # --------------------------------------------------------

    message_text = (
        message.text
        or message.caption
        or ""
    )

    message_bad = has_forbidden_content(message_text)

    # --------------------------------------------------------
    # BIO CHECK
    # --------------------------------------------------------
    #
    # IMPORTANT:
    # The bio is checked on EVERY group message.
    # If the user's bio contains a link, the current message
    # is deleted and a 1/3 warning is sent.
    #
    # This is the behavior shown in your screenshot.
    # --------------------------------------------------------

    bio = await get_user_bio(
        context,
        user.id,
    )

    bio_bad = has_forbidden_content(bio)

    # --------------------------------------------------------
    # NOTHING BAD
    # --------------------------------------------------------

    if not message_bad and not bio_bad:
        return

    # --------------------------------------------------------
    # DELETE OFFENDING MESSAGE
    # --------------------------------------------------------

    await safe_delete_message(
        context,
        chat.id,
        message.message_id,
    )

    # --------------------------------------------------------
    # WARNING
    # --------------------------------------------------------

    count = add_warning(
        chat.id,
        user.id,
    )

    if count < 3:
        if bio_bad and message_bad:
            reason = "your bio and message contain a link."
        elif bio_bad:
            reason = "your bio contains a link."
        else:
            reason = "your message contains a link or username."

        await send_bio_warning(
            context,
            chat.id,
            user,
            count,
            reason,
        )

        return

    # --------------------------------------------------------
    # 3/3 -> MUTE
    # --------------------------------------------------------

    duration = get_mute_duration()

    try:
        await mute_user(
            context,
            chat.id,
            user.id,
            duration,
        )

        reset_warning(
            chat.id,
            user.id,
        )

        await send_mute_notice(
            context,
            chat.id,
            user,
            "3 link/bio warnings reached.",
            f"{duration} hour(s)",
        )

    except Exception as e:
        logger.error(
            "Mute error: %s",
            e,
        )


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    chat = update.effective_chat

    if not user or not update.message:
        return

    save_user(user)

    if chat and chat.type in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        save_group(chat)

    # Force-join is only checked in private chat.
    if chat and chat.type == ChatType.PRIVATE:
        if not await is_subscribed(
            context,
            user.id,
        ):
            channel = channel_username(UPDATE_CHANNEL)

            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔗 ᴊᴏɪɴ ᴜᴘᴅᴀᴛᴇ ᴄʜᴀɴɴᴇʟ",
                            url=normalize_channel_url(
                                UPDATE_CHANNEL
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "✅ ᴄʜᴇᴄᴋ ᴀɢᴀɪɴ",
                            callback_data="start",
                        )
                    ],
                ]
            )

            return await update.message.reply_text(
                "🚫 <b>ᴀᴄᴄᴇss ʀᴇsᴛʀɪᴄᴛᴇᴅ</b>\n\n"
                "Please join the update channel first.\n\n"
                "After joining, press <b>Check Again</b>.",
                parse_mode="HTML",
                reply_markup=keyboard,
            )

    bot_username = await get_bot_username(context)

    caption = (
        "⚔️ <b>ʙɪᴏɢᴜᴀʀᴅ</b> — <i>ᴘʀᴏᴛᴇᴄᴛɪᴏɴ sʏsᴛᴇᴍ</i>\n\n"
        "🛡 <b>ᴀᴅᴠᴀɴᴄᴇᴅ ʙɪᴏ & ʟɪɴᴋ ᴘʀᴏᴛᴇᴄᴛɪᴏɴ</b>\n\n"
        "🚫 <b>ʙɪᴏ ᴄʜᴇᴄᴋ</b> — ʟɪɴᴋs & @ᴜsᴇʀɴᴀᴍᴇs ᴅᴇᴛᴇᴄᴛɪᴏɴ\n"
        "🧹 <b>ᴀᴜᴛᴏ ᴅᴇʟᴇᴛᴇ</b> — ʀᴇᴍᴏᴠᴇ ᴠɪᴏʟᴀᴛɪɴɢ ᴍᴇssᴀɢᴇs\n"
        "⚠️ <b>ᴡᴀʀɴɪɴɢ sʏsᴛᴇᴍ</b> — ᴜᴘ ᴛᴏ 𝟹 ᴡᴀʀɴɪɴɢs\n"
        "🔇 <b>ᴀᴜᴛᴏ ᴍᴜᴛᴇ</b> — ᴍᴜᴛᴇ ᴜsᴇʀ ᴀғᴛᴇʀ 𝟹 ᴡᴀʀɴɪɴɢs\n"
        "⏱ <b>ɴᴏᴛɪᴄᴇ</b> — ᴡᴀʀɴɪɴɢ ᴍᴇssᴀɢᴇ ᴅᴇʟᴇᴛᴇs ᴀғᴛᴇʀ 𝟻 ᴍɪɴᴜᴛᴇs\n"
        "👮 <b>ᴀᴅᴍɪɴ ᴇxᴇᴍᴘᴛɪᴏɴ</b> — ᴛʀᴜsᴛᴇᴅ ᴀᴅᴍɪɴs ᴀʀᴇ sᴋɪᴘᴘᴇᴅ\n\n"
        "✨ <i>ᴋᴇᴇᴘ ʏᴏᴜʀ ɢʀᴏᴜᴘ ᴄʟᴇᴀɴ, sᴀғᴇ & sᴘᴀᴍ-ғʀᴇᴇ.</i>\n\n"
        "💫 <b>ᴀᴅᴅ ᴍᴇ ᴛᴏ ʏᴏᴜʀ ɢʀᴏᴜᴘ ᴀɴᴅ sᴛᴀʀᴛ ᴘʀᴏᴛᴇᴄᴛɪɴɢ ɪᴛ.</b>"
    )

    await update.message.reply_text(
        caption,
        parse_mode="HTML",
        reply_markup=start_keyboard(bot_username),
        disable_web_page_preview=True,
    )


# ============================================================
# HELP
# ============================================================

HELP_TEXT = (
    "🤖 <b>ʙɪᴏɢᴜᴀʀᴅ ʜᴇʟᴘ</b>\n\n"
    "🛡 <b>ɢʀᴏᴜᴘ ᴘʀᴏᴛᴇᴄᴛɪᴏɴ</b>\n"
    "• ʙɪᴏ ʟɪɴᴋ ᴅᴇᴛᴇᴄᴛɪᴏɴ\n"
    "• ᴍᴇssᴀɢᴇ ʟɪɴᴋ ᴅᴇᴛᴇᴄᴛɪᴏɴ\n"
    "• @ᴜsᴇʀɴᴀᴍᴇ ᴅᴇᴛᴇᴄᴛɪᴏɴ\n"
    "• ᴀᴜᴛᴏ ᴡᴀʀɴɪɴɢ\n"
    "• 3 ᴡᴀʀɴɪɴɢs = ᴍᴜᴛᴇ\n"
    "• ᴀᴅᴍɪɴ ᴇxᴇᴍᴘᴛɪᴏɴ\n\n"
    "⏱ <b>ᴡᴀʀɴɪɴɢ ᴍᴇssᴀɢᴇ</b>\n"
    "• ᴀᴜᴛᴏ ᴅᴇʟᴇᴛᴇs ᴀғᴛᴇʀ 5 ᴍɪɴᴜᴛᴇs\n\n"
    "🔗 <b>ʙɪᴏ ʟɪɴᴋ ʀᴜʟᴇ</b>\n"
    "• ɪғ ᴀ ᴜsᴇʀ's ʙɪᴏ ᴄᴏɴᴛᴀɪɴs ᴀ ʟɪɴᴋ, "
    "ᴛʜᴇ ᴜsᴇʀ's ɴᴇxᴛ ᴍᴇssᴀɢᴇ ɪs ᴅᴇʟᴇᴛᴇᴅ\n\n"
    "👑 <b>ᴏᴡɴᴇʀ</b>\n"
    "• /setmute &lt;hours&gt;\n"
    "• /free &lt;user_id&gt;\n"
    "• /unfree &lt;user_id&gt;\n"
    "• /approve &lt;user_id&gt;\n"
    "• /unapprove &lt;user_id&gt;\n"
    "• /status\n"
    "• /broadcast"
)


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    await update.message.reply_text(
        HELP_TEXT,
        parse_mode="HTML",
        reply_markup=help_keyboard(),
        disable_web_page_preview=True,
    )


# ============================================================
# CALLBACKS
# ============================================================

async def callbacks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    await query.answer()

    bot_username = await get_bot_username(context)

    if query.data == "help":
        try:
            await query.edit_message_text(
                HELP_TEXT,
                parse_mode="HTML",
                reply_markup=help_keyboard(),
                disable_web_page_preview=True,
            )
        except Exception:
            pass

    elif query.data == "start":
        start_text = (
            "⚔️ <b>ʙɪᴏɢᴜᴀʀᴅ</b>\n\n"
            "🛡 <b>ᴛᴇʟᴇɢʀᴀᴍ ɢʀᴏᴜᴘ ᴘʀᴏᴛᴇᴄᴛɪᴏɴ</b>\n\n"
            "🚫 ʟɪɴᴋ & ᴜsᴇʀɴᴀᴍᴇ ᴘʀᴏᴛᴇᴄᴛɪᴏɴ\n"
            "🔎 ᴜsᴇʀ ʙɪᴏ ᴄʜᴇᴄᴋ\n"
            "⚠️ ᴀᴜᴛᴏ ᴡᴀʀɴɪɴɢ sʏsᴛᴇᴍ\n"
            "🔇 ᴀᴜᴛᴏ ᴍᴜᴛᴇ sʏsᴛᴇᴍ\n"
            "🛡 ᴀᴅᴍɪɴ ᴇxᴇᴍᴘᴛɪᴏɴ\n\n"
            "💫 <i>ᴋᴇᴇᴘ ʏᴏᴜʀ ɢʀᴏᴜᴘ ᴄʟᴇᴀɴ & sᴀғᴇ.</i>"
        )

        try:
            await query.edit_message_text(
                start_text,
                parse_mode="HTML",
                reply_markup=start_keyboard(bot_username),
                disable_web_page_preview=True,
            )
        except Exception:
            pass


# ============================================================
# OWNER COMMANDS
# ============================================================

def owner_only(user_id):
    return user_id == OWNER_ID


async def setmute_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.message:
        return

    if not owner_only(update.effective_user.id):
        return

    if not context.args:
        return await update.message.reply_text(
            "Usage: /setmute <hours>"
        )

    try:
        hours = int(context.args[0])
        if hours < 1 or hours > 720:
            raise ValueError

        set_mute_duration(hours)

        await update.message.reply_text(
            f"✅ Global mute duration set to {hours} hour(s)."
        )

    except ValueError:
        await update.message.reply_text(
            "❌ Use a number from 1 to 720."
        )


async def free_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.message:
        return

    if not owner_only(update.effective_user.id):
        return

    if not context.args:
        return await update.message.reply_text(
            "Usage: /free <user_id>"
        )

    try:
        user_id = int(context.args[0])
    except ValueError:
        return await update.message.reply_text(
            "❌ Invalid user ID."
        )

    add_approved_user(user_id)

    try:
        user = await context.bot.get_chat(user_id)
        add_free_user(user)
    except Exception:
        # Approval itself is enough even if user profile cannot be fetched.
        pass

    await update.message.reply_text(
        f"✅ User <code>{user_id}</code> is now exempt.",
        parse_mode="HTML",
    )


async def unfree_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.message:
        return

    if not owner_only(update.effective_user.id):
        return

    if not context.args:
        return await update.message.reply_text(
            "Usage: /unfree <user_id>"
        )

    try:
        user_id = int(context.args[0])
    except ValueError:
        return await update.message.reply_text(
            "❌ Invalid user ID."
        )

    remove_free_user(user_id)
    remove_approved_user(user_id)

    await update.message.reply_text(
        f"✅ User <code>{user_id}</code> removed from exemption.",
        parse_mode="HTML",
    )


async def approve_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.message:
        return

    if not owner_only(update.effective_user.id):
        return

    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
        user_id = target.id
    elif context.args:
        try:
            user_id = int(context.args[0])
        except ValueError:
            return await update.message.reply_text(
                "❌ Invalid user ID."
            )
    else:
        return await update.message.reply_text(
            "Usage: /approve <user_id>\n"
            "or reply to a user's message with /approve"
        )

    add_approved_user(user_id)

    await update.message.reply_text(
        f"✅ User <code>{user_id}</code> approved for links.",
        parse_mode="HTML",
    )


async def unapprove_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.message:
        return

    if not owner_only(update.effective_user.id):
        return

    if not context.args:
        return await update.message.reply_text(
            "Usage: /unapprove <user_id>"
        )

    try:
        user_id = int(context.args[0])
    except ValueError:
        return await update.message.reply_text(
            "❌ Invalid user ID."
        )

    remove_approved_user(user_id)

    await update.message.reply_text(
        f"✅ Link approval removed for <code>{user_id}</code>.",
        parse_mode="HTML",
    )


async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.message:
        return

    if not owner_only(update.effective_user.id):
        return

    db_type = "MongoDB" if mongo_db is not None else "SQLite fallback"

    text = (
        "📊 <b>ʙɪᴏɢᴜᴀʀᴅ sᴛᴀᴛᴜs</b>\n\n"
        f"👤 Users: <code>{total_users()}</code>\n"
        f"👥 Groups: <code>{total_groups()}</code>\n"
        f"🔇 Mute: <code>{get_mute_duration()}h</code>\n"
        f"⏱ Warning delete: <code>{NOTICE_DELETE_SECONDS}s</code>\n"
        f"💾 Database: <code>{db_type}</code>"
    )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
    )


async def broadcast_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.message:
        return

    if not owner_only(update.effective_user.id):
        return

    if not update.message.reply_to_message:
        return await update.message.reply_text(
            "❌ Reply to the message you want to broadcast."
        )

    source = update.message.reply_to_message

    targets = list(dict.fromkeys(
        iter_user_ids() + iter_group_ids()
    ))

    sent = 0
    failed = 0

    status = await update.message.reply_text(
        f"📢 Broadcasting to {len(targets)} targets..."
    )

    for target in targets:
        try:
            await context.bot.copy_message(
                chat_id=target,
                from_chat_id=source.chat_id,
                message_id=source.message_id,
            )
            sent += 1
        except Exception:
            failed += 1

        await asyncio.sleep(0.05)

    try:
        await status.edit_text(
            "📢 <b>Broadcast finished</b>\n\n"
            f"✅ Sent: <code>{sent}</code>\n"
            f"❌ Failed: <code>{failed}</code>",
            parse_mode="HTML",
        )
    except Exception:
        pass


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error(
        "Unhandled exception: %s",
        context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():
    logger.info("Starting BioGuard / BioMuteBot...")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    # Commands
    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("help", help_command)
    )

    app.add_handler(
        CommandHandler("setmute", setmute_command)
    )

    app.add_handler(
        CommandHandler("free", free_command)
    )

    app.add_handler(
        CommandHandler("unfree", unfree_command)
    )

    app.add_handler(
        CommandHandler("approve", approve_command)
    )

    app.add_handler(
        CommandHandler("unapprove", unapprove_command)
    )

    app.add_handler(
        CommandHandler("status", status_command)
    )

    app.add_handler(
        CommandHandler("broadcast", broadcast_command)
    )

    # Buttons
    app.add_handler(
        CallbackQueryHandler(callbacks)
    )

    # Group moderation.
    # This catches text/captions, including ordinary messages.
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & ~filters.COMMAND,
            check_message,
        ),
        group=10,
    )

    app.add_error_handler(error_handler)

    logger.info(
        "Bot started. Bio warning messages delete after %s seconds.",
        NOTICE_DELETE_SECONDS,
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
