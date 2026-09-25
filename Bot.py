# ============================================================
#                 BIOGUARD / BIOMUTEBOT
#          Telegram Bio + Link Protection Bot
# ============================================================

import logging
import os
import re
from datetime import datetime, timedelta, timezone

from pymongo import MongoClient
from pymongo.errors import PyMongoError

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

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID_RAW = os.getenv("OWNER_ID")

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "bioguard")

UPDATE_CHANNEL = os.getenv(
    "UPDATE_CHANNEL",
    "https://t.me/annu_support",
)

SUPPORT_CHANNEL = os.getenv(
    "SUPPORT_CHANNEL",
    "https://t.me/annu_updates",
)

# Default mute duration
DEFAULT_MUTE_HOURS = int(
    os.getenv("MUTE_DURATION", "2")
)

# ============================================================
# REQUIRED CONFIG CHECK
# ============================================================

if not BOT_TOKEN:
    raise ValueError(
        "BOT_TOKEN is missing. Add BOT_TOKEN in Heroku Config Vars."
    )

if not OWNER_ID_RAW:
    raise ValueError(
        "OWNER_ID is missing. Add OWNER_ID in Heroku Config Vars."
    )

if not MONGO_URI:
    raise ValueError(
        "MONGO_URI is missing. Add MONGO_URI in Heroku Config Vars."
    )

try:
    OWNER_ID = int(OWNER_ID_RAW)
except ValueError:
    raise ValueError(
        "OWNER_ID must contain a numeric Telegram user ID."
    )

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("BioGuard")

# ============================================================
# MONGODB
# ============================================================

try:
    mongo = MongoClient(
        MONGO_URI,
        serverSelectionTimeoutMS=10000,
        connectTimeoutMS=10000,
        socketTimeoutMS=10000,
    )

    # Test connection
    mongo.admin.command("ping")

    db = mongo[MONGO_DB]

    users_collection = db["users"]
    groups_collection = db["groups"]
    warnings_collection = db["warnings"]
    settings_collection = db["settings"]
    free_users_collection = db["free_users"]

    settings_collection.update_one(
        {"_id": "global"},
        {
            "$setOnInsert": {
                "mute_duration": DEFAULT_MUTE_HOURS,
            }
        },
        upsert=True,
    )

    logger.info(
        "MongoDB connected successfully | Database: %s",
        MONGO_DB,
    )

except PyMongoError as e:
    logger.exception("MongoDB connection failed: %s", e)
    raise

# ============================================================
# DATABASE HELPERS
# ============================================================


def now_utc():
    return datetime.now(timezone.utc)


def save_user(user):
    if not user:
        return

    try:
        users_collection.update_one(
            {"_id": user.id},
            {
                "$set": {
                    "id": user.id,
                    "first_name": user.first_name or "",
                    "last_name": user.last_name or "",
                    "username": user.username or "",
                    "is_bot": bool(user.is_bot),
                    "updated_at": now_utc(),
                }
            },
            upsert=True,
        )
    except PyMongoError as e:
        logger.warning("save_user error: %s", e)


def save_group(chat):
    if not chat:
        return

    try:
        groups_collection.update_one(
            {"_id": chat.id},
            {
                "$set": {
                    "id": chat.id,
                    "title": chat.title or "",
                    "username": chat.username or "",
                    "type": str(chat.type),
                    "updated_at": now_utc(),
                }
            },
            upsert=True,
        )
    except PyMongoError as e:
        logger.warning("save_group error: %s", e)


def get_mute_duration():
    try:
        data = settings_collection.find_one(
            {"_id": "global"}
        )

        if not data:
            return DEFAULT_MUTE_HOURS

        return max(
            1,
            int(
                data.get(
                    "mute_duration",
                    DEFAULT_MUTE_HOURS,
                )
            ),
        )

    except Exception:
        return DEFAULT_MUTE_HOURS


def set_mute_duration(hours):
    hours = max(1, int(hours))

    settings_collection.update_one(
        {"_id": "global"},
        {
            "$set": {
                "mute_duration": hours,
            }
        },
        upsert=True,
    )


def get_warning(chat_id, user_id):
    try:
        data = warnings_collection.find_one(
            {
                "chat_id": chat_id,
                "user_id": user_id,
            }
        )

        if not data:
            return 0

        return int(data.get("count", 0))

    except Exception:
        return 0


def add_warning(chat_id, user_id):
    old = get_warning(chat_id, user_id)
    new_count = old + 1

    warnings_collection.update_one(
        {
            "chat_id": chat_id,
            "user_id": user_id,
        },
        {
            "$set": {
                "count": new_count,
                "updated_at": now_utc(),
            }
        },
        upsert=True,
    )

    return new_count


def reset_warning(chat_id, user_id):
    try:
        warnings_collection.delete_one(
            {
                "chat_id": chat_id,
                "user_id": user_id,
            }
        )
    except PyMongoError as e:
        logger.warning(
            "reset_warning error: %s",
            e,
        )

# ============================================================
# FREE USER SYSTEM
# ============================================================


def is_free_user(user_id):
    try:
        return bool(
            free_users_collection.find_one(
                {"_id": user_id}
            )
        )
    except Exception:
        return False


def add_free_user(user):
    if not user:
        return

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


def remove_free_user(user_id):
    free_users_collection.delete_one(
        {"_id": user_id}
    )

# ============================================================
# LINK / USERNAME DETECTION
# ============================================================

LINK_REGEX = re.compile(
    r"("
    r"https?://\S+"
    r"|www\.\S+"
    r"|t\.me/\S+"
    r"|telegram\.me/\S+"
    r"|telegram\.dog/\S+"
    r"|instagram\.com/\S*"
    r"|facebook\.com/\S*"
    r"|twitter\.com/\S*"
    r"|x\.com/\S*"
    r"|youtube\.com/\S*"
    r"|youtu\.be/\S*"
    r"|wa\.me/\S*"
    r"|whatsapp\.com/\S*"
    r")",
    re.IGNORECASE,
)

USERNAME_REGEX = re.compile(
    r"(?<!\w)@[A-Za-z0-9_]{4,32}"
)


def has_link(text):
    if not text:
        return False

    return bool(
        LINK_REGEX.search(text)
    )


def has_username(text):
    if not text:
        return False

    return bool(
        USERNAME_REGEX.search(text)
    )


def has_forbidden_content(text):
    if not text:
        return False

    return (
        has_link(text)
        or has_username(text)
    )

# ============================================================
# TELEGRAM HELPERS
# ============================================================


async def get_bot_username(context):
    try:
        me = await context.bot.get_me()
        return me.username or ""
    except TelegramError:
        return ""


async def get_user_bio(context, user_id):
    try:
        chat = await context.bot.get_chat(
            user_id
        )
        return chat.bio or ""
    except TelegramError:
        return ""


async def is_subscribed(context, user_id):
    """
    Checks update channel membership.

    If UPDATE_CHANNEL is empty, subscription check is disabled.
    """

    if not UPDATE_CHANNEL:
        return True

    try:
        member = await context.bot.get_chat_member(
            UPDATE_CHANNEL,
            user_id,
        )

        return member.status in (
            "member",
            "administrator",
            "creator",
        )

    except TelegramError as e:
        logger.warning(
            "Subscription check failed: %s",
            e,
        )

        # Do not lock everyone out if the channel check
        # itself cannot be completed.
        return True


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

    except TelegramError:
        return False

# ============================================================
# URL HELPERS
# ============================================================


def normalize_channel_url(value):
    if not value:
        return ""

    value = value.strip()

    if value.startswith("https://t.me/"):
        return value

    if value.startswith("http://t.me/"):
        return value.replace(
            "http://",
            "https://",
            1,
        )

    if value.startswith("@"):
        return (
            "https://t.me/"
            + value[1:]
        )

    return (
        "https://t.me/"
        + value
    )

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
                        "https://t.me/"
                        f"{bot_username}"
                        "?startgroup=true"
                    ),
                )
            ]
        )

    second_row = []

    update_url = normalize_channel_url(
        UPDATE_CHANNEL
    )

    support_url = normalize_channel_url(
        SUPPORT_CHANNEL
    )

    if update_url:
        second_row.append(
            InlineKeyboardButton(
                "🔄 ᴜᴘᴅᴀᴛᴇ",
                url=update_url,
            )
        )

    if support_url:
        second_row.append(
            InlineKeyboardButton(
                "💬 sᴜᴘᴘᴏʀᴛ",
                url=support_url,
            )
        )

    if second_row:
        rows.append(second_row)

    rows.append(
        [
            InlineKeyboardButton(
                "❔ ʜᴇʟᴘ",
                callback_data="help",
            )
        ]
    )

    return InlineKeyboardMarkup(rows)


def help_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔗 ʟɪɴᴋ ᴘʀᴏᴛᴇᴄᴛɪᴏɴ",
                    callback_data="links",
                ),
                InlineKeyboardButton(
                    "👤 ʙɪᴏ ᴘʀᴏᴛᴇᴄᴛɪᴏɴ",
                    callback_data="bio",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⚠️ ᴡᴀʀɴɪɴɢ sʏsᴛᴇᴍ",
                    callback_data="warnings",
                ),
                InlineKeyboardButton(
                    "🔇 ᴍᴜᴛᴇ sʏsᴛᴇᴍ",
                    callback_data="mute",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔙 ʙᴀᴄᴋ",
                    callback_data="start",
                )
            ],
        ]
    )


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


def mute_keyboard(bot_username):

    rows = []

    update_url = normalize_channel_url(
        UPDATE_CHANNEL
    )

    support_url = normalize_channel_url(
        SUPPORT_CHANNEL
    )

    if update_url:
        rows.append(
            [
                InlineKeyboardButton(
                    "🔄 ᴜᴘᴅᴀᴛᴇ",
                    url=update_url,
                )
            ]
        )

    if support_url:
        rows.append(
            [
                InlineKeyboardButton(
                    "💬 sᴜᴘᴘᴏʀᴛ",
                    url=support_url,
                )
            ]
        )

    if bot_username:
        rows.append(
            [
                InlineKeyboardButton(
                    "🔓 ᴜɴᴍᴜᴛᴇ ʙᴏᴛ",
                    url=(
                        f"https://t.me/"
                        f"{bot_username}"
                    ),
                )
            ]
        )

    return InlineKeyboardMarkup(rows)

# ============================================================
# MUTE / UNMUTE
# ============================================================


async def mute_user(
    context,
    chat_id,
    user_id,
    hours=None,
):

    if hours is None:
        hours = get_mute_duration()

    until = (
        now_utc()
        + timedelta(hours=hours)
    )

    permissions = ChatPermissions(
        can_send_messages=False
    )

    await context.bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        permissions=permissions,
        until_date=until,
    )


async def permanent_mute(
    context,
    chat_id,
    user_id,
):

    permissions = ChatPermissions(
        can_send_messages=False
    )

    await context.bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        permissions=permissions,
    )


async def unmute_user(
    context,
    chat_id,
    user_id,
):

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
# MUTE NOTICE
# ============================================================


async def send_mute_notice(
    context,
    chat_id,
    user,
    reason,
    duration_text,
):

    bot_username = await get_bot_username(
        context
    )

    safe_name = (
        user.first_name
        or "User"
    )

    text = (
        "⚔️ <b>ʙɪᴏɢᴜᴀʀᴅ ᴍᴜᴛᴇ</b>\n\n"
        f"👤 <b>ᴜsᴇʀ:</b> {safe_name}\n"
        f"🆔 <b>ɪᴅ:</b> "
        f"<code>{user.id}</code>\n\n"
        f"⛔ <b>ʀᴇᴀsᴏɴ:</b> {reason}\n"
        f"⏱ <b>ᴅᴜʀᴀᴛɪᴏɴ:</b> "
        f"{duration_text}"
    )

    markup = mute_keyboard(
        bot_username
    )

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
        )
    except TelegramError as e:
        logger.warning(
            "Mute notice error: %s",
            e,
        )

    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
        )
    except TelegramError:
        pass

# ============================================================
# MAIN MODERATION
# ============================================================


async def check_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.effective_message

    if not message:
        return

    user = message.from_user

    if not user:
        return

    chat = update.effective_chat

    if not chat:
        return

    save_user(user)

    # --------------------------------------------------------
    # GROUP ONLY
    # --------------------------------------------------------

    if chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        return

    save_group(chat)

    # --------------------------------------------------------
    # BOT / OWNER
    # --------------------------------------------------------

    if user.is_bot:
        return

    if user.id == OWNER_ID:
        return

    # --------------------------------------------------------
    # ADMIN EXEMPTION
    # --------------------------------------------------------

    if await is_admin(
        context,
        chat.id,
        user.id,
    ):
        return

    # --------------------------------------------------------
    # FREE USER
    # --------------------------------------------------------

    if is_free_user(user.id):
        return

    # --------------------------------------------------------
    # NAME CHECK
    # --------------------------------------------------------

    full_name = " ".join(
        filter(
            None,
            [
                user.first_name,
                user.last_name,
            ],
        )
    )

    if has_forbidden_content(
        full_name
    ):

        try:

            await permanent_mute(
                context,
                chat.id,
                user.id,
            )

            try:
                await message.delete()
            except TelegramError:
                pass

            reset_warning(
                chat.id,
                user.id,
            )

            await send_mute_notice(
                context,
                chat.id,
                user,
                "Link or username detected in name.",
                "ᴘᴇʀᴍᴀɴᴇɴᴛ",
            )

        except TelegramError as e:

            logger.error(
                "Name mute error: %s",
                e,
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

    message_bad = has_forbidden_content(
        message_text
    )

    # --------------------------------------------------------
    # BIO CHECK
    # --------------------------------------------------------

    bio = await get_user_bio(
        context,
        user.id,
    )

    bio_bad = has_forbidden_content(
        bio
    )

    # --------------------------------------------------------
    # NOTHING FOUND
    # --------------------------------------------------------

    if not message_bad and not bio_bad:
        return

    # --------------------------------------------------------
    # DELETE MESSAGE
    # --------------------------------------------------------

    if message_bad:

        try:
            await message.delete()
        except TelegramError:
            pass

    # --------------------------------------------------------
    # WARNING
    # --------------------------------------------------------

    count = add_warning(
        chat.id,
        user.id,
    )

    if count <= 3:

        text = (
            "⚠️ <b>ʟɪɴᴋ ᴡᴀʀɴɪɴɢ</b>\n\n"
            f"👤 <b>{user.first_name or 'User'}</b>\n\n"
            "🚫 Links/usernames are not "
            "allowed in your bio or messages.\n\n"
            f"⚠️ <b>ᴡᴀʀɴɪɴɢ:</b> "
            f"{count}/3\n\n"
            "🔇 3 warnings = automatic mute."
        )

        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=text,
                parse_mode="HTML",
            )
        except TelegramError:
            pass

        try:
            await context.bot.send_message(
                chat_id=user.id,
                text=text,
                parse_mode="HTML",
            )
        except TelegramError:
            pass

        # On third warning mute immediately.
        if count < 3:
            return

    # --------------------------------------------------------
    # MUTE
    # --------------------------------------------------------

    duration = get_mute_duration()

    try:

        await mute_user(
            context,
            chat.id,
            user.id,
            duration,
        )

        await send_mute_notice(
            context,
            chat.id,
            user,
            "3 link/bio warnings reached.",
            f"{duration} hour(s)",
        )

        reset_warning(
            chat.id,
            user.id,
        )

    except TelegramError as e:

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

    if not user or not chat:
        return

    save_user(user)

    if chat.type in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        save_group(chat)

    # --------------------------------------------------------
    # PRIVATE SUBSCRIPTION CHECK
    # --------------------------------------------------------

    if chat.type == ChatType.PRIVATE:

        subscribed = await is_subscribed(
            context,
            user.id,
        )

        if not subscribed:

            channel_url = normalize_channel_url(
                UPDATE_CHANNEL
            )

            keyboard = []

            if channel_url:
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            "🔗 ᴊᴏɪɴ ᴜᴘᴅᴀᴛᴇ ᴄʜᴀɴɴᴇʟ",
                            url=channel_url,
                        )
                    ]
                )

            keyboard.append(
                [
                    InlineKeyboardButton(
                        "🔄 ᴄʜᴇᴄᴋ ᴀɢᴀɪɴ",
                        callback_data="start",
                    )
                ]
            )

            return await update.message.reply_text(
                "🚫 <b>ᴀᴄᴄᴇss ʀᴇsᴛʀɪᴄᴛᴇᴅ</b>\n\n"
                "Please join our update channel first.\n\n"
                "After joining, press "
                "<b>Check Again</b>.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    keyboard
                ),
            )

    bot_username = await get_bot_username(
        context
    )

    # --------------------------------------------------------
    # START TEXT
    # --------------------------------------------------------

    caption = (
        "⚔️ <b>ʙɪᴏɢᴜᴀʀᴅ</b>\n\n"
        "🛡 <b>ᴛᴇʟᴇɢʀᴀᴍ ɢʀᴏᴜᴘ "
        "ᴘʀᴏᴛᴇᴄᴛɪᴏɴ</b>\n\n"
        "🚫 ʟɪɴᴋ & ᴜsᴇʀɴᴀᴍᴇ ᴘʀᴏᴛᴇᴄᴛɪᴏɴ\n"
        "🔎 ᴜsᴇʀ ʙɪᴏ ᴄʜᴇᴄᴋ\n"
        "⚠️ ᴀᴜᴛᴏ ᴡᴀʀɴɪɴɢ sʏsᴛᴇᴍ\n"
        "🔇 ᴀᴜᴛᴏ ᴍᴜᴛᴇ sʏsᴛᴇᴍ\n"
        "🛡 ᴀᴅᴍɪɴ ᴇxᴇᴍᴘᴛɪᴏɴ\n"
        "✨ ᴘᴇʀsɪsᴛᴇɴᴛ ᴍᴏɴɪᴛᴏʀɪɴɢ\n\n"
        "💫 <i>ᴋᴇᴇᴘ ʏᴏᴜʀ ɢʀᴏᴜᴘ "
        "ᴄʟᴇᴀɴ & sᴀғᴇ.</i>"
    )

    if update.message:

        await update.message.reply_text(
            caption,
            parse_mode="HTML",
            reply_markup=start_keyboard(
                bot_username
            ),
        )

# ============================================================
# HELP
# ============================================================


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    text = (
        "❔ <b>ʙɪᴏɢᴜᴀʀᴅ ʜᴇʟᴘ</b>\n\n"
        "🛡 <b>ʙɪᴏɢᴜᴀʀᴅ</b> protects groups "
        "from unwanted links and usernames.\n\n"
        "🔗 <b>ʟɪɴᴋ ᴘʀᴏᴛᴇᴄᴛɪᴏɴ</b>\n"
        "Detects links and usernames in "
        "messages and captions.\n\n"
        "👤 <b>ʙɪᴏ ᴘʀᴏᴛᴇᴄᴛɪᴏɴ</b>\n"
        "Checks user bios for forbidden "
        "links/usernames.\n\n"
        "⚠️ <b>ᴡᴀʀɴɪɴɢ</b>\n"
        "Users receive up to 3 warnings.\n\n"
        "🔇 <b>ᴍᴜᴛᴇ</b>\n"
        "After 3 warnings, the user is "
        "automatically muted.\n\n"
        "👑 <b>ᴀᴅᴍɪɴs</b>\n"
        "Group administrators are exempt."
    )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=help_keyboard(),
    )

# ============================================================
# STATUS
# ============================================================


async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    user = update.effective_user

    if not user:
        return

    text = (
        "⚔️ <b>ʙɪᴏɢᴜᴀʀᴅ sᴛᴀᴛᴜs</b>\n\n"
        f"👤 <b>ᴜsᴇʀ:</b> "
        f"{user.first_name or 'User'}\n"
        f"🆔 <b>ɪᴅ:</b> "
        f"<code>{user.id}</code>\n\n"
        f"⚠️ <b>ᴡᴀʀɴɪɴɢs:</b> "
        f"{get_warning(update.effective_chat.id, user.id) "
        if update.effective_chat else 0}/3\n"
        f"⏱ <b>ᴍᴜᴛᴇ ᴅᴜʀᴀᴛɪᴏɴ:</b> "
        f"{get_mute_duration()} hour(s)"
    )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
    )

# ============================================================
# OWNER COMMANDS
# ============================================================


async def free_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if not user or user.id != OWNER_ID:
        return

    if not context.args:
        return await update.message.reply_text(
            "Usage:\n"
            "/free <user_id>"
        )

    try:
        user_id = int(context.args[0])
        add_free_user(
            type(
                "User",
                (),
                {
                    "id": user_id,
                    "first_name": "",
                    "username": "",
                },
            )()
        )

        await update.message.reply_text(
            f"✅ User <code>{user_id}</code> "
            "added to free list.",
            parse_mode="HTML",
        )

    except ValueError:
        await update.message.reply_text(
            "❌ Invalid user ID."
        )


async def unfree_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if not user or user.id != OWNER_ID:
        return

    if not context.args:
        return await update.message.reply_text(
            "Usage:\n"
            "/unfree <user_id>"
        )

    try:
        user_id = int(context.args[0])

        remove_free_user(user_id)

        await update.message.reply_text(
            f"✅ User <code>{user_id}</code> "
            "removed from free list.",
            parse_mode="HTML",
        )

    except ValueError:
        await update.message.reply_text(
            "❌ Invalid user ID."
        )


async def setmute_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if not user or user.id != OWNER_ID:
        return

    if not context.args:
        return await update.message.reply_text(
            f"Current mute duration: "
            f"{get_mute_duration()} hour(s)\n\n"
            "Usage:\n"
            "/setmute <hours>"
        )

    try:
        hours = int(context.args[0])

        if hours < 1:
            raise ValueError

        set_mute_duration(hours)

        await update.message.reply_text(
            f"✅ Mute duration set to "
            f"<b>{hours} hour(s)</b>.",
            parse_mode="HTML",
        )

    except ValueError:
        await update.message.reply_text(
            "❌ Enter a valid number greater than 0."
        )

# ============================================================
# CALLBACKS
# ============================================================


async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    if not query:
        return

    await query.answer()

    data = query.data

    bot_username = await get_bot_username(
        context
    )

    if data == "start":

        text = (
            "⚔️ <b>ʙɪᴏɢᴜᴀʀᴅ</b>\n\n"
            "🛡 <b>ᴛᴇʟᴇɢʀᴀᴍ ɢʀᴏᴜᴘ "
            "ᴘʀᴏᴛᴇᴄᴛɪᴏɴ</b>\n\n"
            "🚫 ʟɪɴᴋ & ᴜsᴇʀɴᴀᴍᴇ ᴘʀᴏᴛᴇᴄᴛɪᴏɴ\n"
            "🔎 ᴜsᴇʀ ʙɪᴏ ᴄʜᴇᴄᴋ\n"
            "⚠️ ᴀᴜᴛᴏ ᴡᴀʀɴɪɴɢ sʏsᴛᴇᴍ\n"
            "🔇 ᴀᴜᴛᴏ ᴍᴜᴛᴇ sʏsᴛᴇᴍ\n"
            "🛡 ᴀᴅᴍɪɴ ᴇxᴇᴍᴘᴛɪᴏɴ\n"
            "✨ ᴘᴇʀsɪsᴛᴇɴᴛ ᴍᴏɴɪᴛᴏʀɪɴɢ"
        )

        try:
            await query.edit_message_text(
                text,
                parse_mode="HTML",
                reply_markup=start_keyboard(
                    bot_username
                ),
            )
        except TelegramError:
            pass

        return

    if data == "help":

        text = (
            "❔ <b>ʙɪᴏɢᴜᴀʀᴅ ʜᴇʟᴘ</b>\n\n"
            "Choose a feature below."
        )

        try:
            await query.edit_message_text(
                text,
                parse_mode="HTML",
                reply_markup=help_keyboard(),
            )
        except TelegramError:
            pass

        return

    if data == "links":

        text = (
            "🔗 <b>ʟɪɴᴋ ᴘʀᴏᴛᴇᴄᴛɪᴏɴ</b>\n\n"
            "BioGuard detects common links "
            "and Telegram usernames.\n\n"
            "Supported examples:\n"
            "• t.me links\n"
            "• http / https links\n"
            "• Instagram\n"
            "• Facebook\n"
            "• Twitter / X\n"
            "• YouTube\n"
            "• WhatsApp\n"
            "• @usernames\n\n"
            "Detected messages are deleted "
            "and warnings are recorded."
        )

        try:
            await query.edit_message_text(
                text,
                parse_mode="HTML",
                reply_markup=back_keyboard(),
            )
        except TelegramError:
            pass

        return

    if data == "bio":

        text = (
            "👤 <b>ʙɪᴏ ᴘʀᴏᴛᴇᴄᴛɪᴏɴ</b>\n\n"
            "BioGuard checks the user's Telegram "
            "bio for links and usernames.\n\n"
            "If forbidden content is found, "
            "the warning system is triggered."
        )

        try:
            await query.edit_message_text(
                text,
                parse_mode="HTML",
                reply_markup=back_keyboard(),
            )
        except TelegramError:
            pass

        return

    if data == "warnings":

        text = (
            "⚠️ <b>ᴡᴀʀɴɪɴɢ sʏsᴛᴇᴍ</b>\n\n"
            "1️⃣ First warning\n"
            "2️⃣ Second warning\n"
            "3️⃣ Third warning → mute\n\n"
            "After the automatic mute, "
            "the warning counter is reset."
        )

        try:
            await query.edit_message_text(
                text,
                parse_mode="HTML",
                reply_markup=back_keyboard(),
            )
        except TelegramError:
            pass

        return

    if data == "mute":

        text = (
            "🔇 <b>ᴍᴜᴛᴇ sʏsᴛᴇᴍ</b>\n\n"
            f"Default mute duration: "
            f"<b>{get_mute_duration()} hour(s)</b>\n\n"
            "After 3 warnings, BioGuard "
            "automatically restricts the user."
        )

        try:
            await query.edit_message_text(
                text,
                parse_mode="HTML",
                reply_markup=back_keyboard(),
            )
        except TelegramError:
            pass

# ============================================================
# ERROR HANDLER
# ============================================================


async def error_handler(
    update,
    context,
):

    logger.exception(
        "Unhandled exception:",
        exc_info=context.error,
    )

# ============================================================
# POST INIT
# ============================================================


async def post_init(application):

    try:

        me = await application.bot.get_me()

        logger.info(
            "Bot started successfully: @%s",
            me.username,
        )

    except TelegramError as e:

        logger.error(
            "Bot startup check failed: %s",
            e,
        )

# ============================================================
# MAIN
# ============================================================


def main():

    logger.info(
        "Starting BioGuard..."
    )

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # --------------------------------------------------------
    # COMMANDS
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "status",
            status_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "free",
            free_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "unfree",
            unfree_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "setmute",
            setmute_command,
        )
    )

    # --------------------------------------------------------
    # CALLBACKS
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    # --------------------------------------------------------
    # MODERATION
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            (
                filters.TEXT
                | filters.CAPTION
            )
            & ~filters.COMMAND,
            check_message,
        )
    )

    # --------------------------------------------------------
    # ERRORS
    # --------------------------------------------------------

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "All handlers loaded successfully."
    )

    # --------------------------------------------------------
    # RUN
    # --------------------------------------------------------

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
