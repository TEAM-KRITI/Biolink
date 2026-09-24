# ============================================================
#                 BIOGUARD / BIOMUTEBOT
#          Telegram Bio + Link Protection Bot
# ============================================================

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from pymongo import MongoClient

from telegram import (
    Update,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatType
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

BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = os.environ.get("OWNER_ID")
UPDATE_CHANNEL = os.environ.get("UPDATE_CHANNEL", "https://t.me/annu_support")
SUPPORT_CHANNEL = os.environ.get("SUPPORT_CHANNEL", "https://t.me/annu_updates")

MONGO_URI = os.environ.get("MONGO_URI")
MONGO_DB = os.environ.get("MONGO_DB", "bioguard")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is missing.")

if not OWNER_ID:
    raise ValueError("OWNER_ID is missing.")

if not MONGO_URI:
    raise ValueError("MONGO_URI is missing.")

OWNER_ID = int(OWNER_ID)

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

mongo = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=10000,
)

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
            "mute_duration": 2,
        }
    },
    upsert=True,
)

# ============================================================
# DATABASE FUNCTIONS
# ============================================================


def save_user(user):

    if not user:
        return

    users_collection.update_one(
        {"_id": user.id},
        {
            "$set": {
                "id": user.id,
                "first_name": user.first_name or "",
                "last_name": user.last_name or "",
                "username": user.username or "",
                "updated_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )


def save_group(chat):

    if not chat:
        return

    groups_collection.update_one(
        {"_id": chat.id},
        {
            "$set": {
                "id": chat.id,
                "title": chat.title or "",
                "username": chat.username or "",
                "updated_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )


def get_mute_duration():

    data = settings_collection.find_one(
        {"_id": "global"}
    )

    if not data:
        return 2

    return int(data.get("mute_duration", 2))


def set_mute_duration(hours):

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

    data = warnings_collection.find_one(
        {
            "chat_id": chat_id,
            "user_id": user_id,
        }
    )

    if not data:
        return 0

    return int(data.get("count", 0))


def add_warning(chat_id, user_id):

    old = get_warning(
        chat_id,
        user_id,
    )

    new_count = old + 1

    warnings_collection.update_one(
        {
            "chat_id": chat_id,
            "user_id": user_id,
        },
        {
            "$set": {
                "count": new_count,
                "updated_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )

    return new_count


def reset_warning(chat_id, user_id):

    warnings_collection.delete_one(
        {
            "chat_id": chat_id,
            "user_id": user_id,
        }
    )


# ============================================================
# FREE USER SYSTEM
# ============================================================


def is_free_user(user_id):

    return bool(
        free_users_collection.find_one(
            {"_id": user_id}
        )
    )


def add_free_user(user):

    free_users_collection.update_one(
        {"_id": user.id},
        {
            "$set": {
                "user_id": user.id,
                "first_name": user.first_name or "",
                "username": user.username or "",
                "added_at": datetime.now(timezone.utc),
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
# BOT INFO
# ============================================================


async def get_bot_username(context):

    try:

        me = await context.bot.get_me()

        return me.username or ""

    except Exception:

        return ""


# ============================================================
# BIO
# ============================================================


async def get_user_bio(
    context,
    user_id,
):

    try:

        chat = await context.bot.get_chat(
            user_id
        )

        return chat.bio or ""

    except Exception:

        return ""


# ============================================================
# SUBSCRIPTION
# ============================================================


async def is_subscribed(
    context,
    user_id,
):

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

    except Exception:

        return False


# ============================================================
# ADMIN CHECK
# ============================================================


async def is_admin(
    context,
    chat_id,
    user_id,
):

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

    rows = [
        [
            InlineKeyboardButton(
                "➕ ᴀᴅᴅ ᴍᴇ ᴛᴏ ɢʀᴏᴜᴘ",
                url=(
                    f"https://t.me/"
                    f"{bot_username}"
                    f"?startgroup=true"
                ),
            )
        ]
    ]

    second_row = []

    if UPDATE_CHANNEL:

        second_row.append(
            InlineKeyboardButton(
                "🔄 ᴜᴘᴅᴀᴛᴇ",
                url=(
                    f"https://t.me/"
                    f"{UPDATE_CHANNEL.lstrip('@')}"
                ),
            )
        )

    if SUPPORT_CHANNEL:

        second_row.append(
            InlineKeyboardButton(
                "💬 sᴜᴘᴘᴏʀᴛ",
                url=(
                    f"https://t.me/"
                    f"{SUPPORT_CHANNEL.lstrip('@')}"
                ),
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


def back_keyboard(bot_username):

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

    if UPDATE_CHANNEL:

        rows.append(
            [
                InlineKeyboardButton(
                    "🔄 ᴜᴘᴅᴀᴛᴇ",
                    url=(
                        f"https://t.me/"
                        f"{UPDATE_CHANNEL.lstrip('@')}"
                    ),
                )
            ]
        )

    if SUPPORT_CHANNEL:

        rows.append(
            [
                InlineKeyboardButton(
                    "💬 sᴜᴘᴘᴏʀᴛ",
                    url=(
                        f"https://t.me/"
                        f"{SUPPORT_CHANNEL.lstrip('@')}"
                    ),
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "🔓 ᴜɴᴍᴜᴛᴇ ʙᴏᴛ",
                url=f"https://t.me/{bot_username}",
            )
        ]
    )

    return InlineKeyboardMarkup(rows)


# ============================================================
# MUTE
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
        datetime.now(timezone.utc)
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


# ============================================================
# PERMANENT MUTE
# ============================================================


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


# ============================================================
# UNMUTE
# ============================================================


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

    text = (
        "⚔️ <b>ʙɪᴏɢᴜᴀʀᴅ ᴍᴜᴛᴇ</b>\n\n"
        f"👤 <b>ᴜsᴇʀ:</b> {user.first_name}\n"
        f"🆔 <b>ɪᴅ:</b> <code>{user.id}</code>\n\n"
        f"⛔ <b>ʀᴇᴀsᴏɴ:</b> {reason}\n"
        f"⏱ <b>ᴅᴜʀᴀᴛɪᴏɴ:</b> {duration_text}"
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

    except Exception as e:

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

    except Exception:

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
    # BOT USERS / OWNER
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
    # USER NAME
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

    if has_forbidden_content(full_name):

        try:

            await permanent_mute(
                context,
                chat.id,
                user.id,
            )

            try:
                await message.delete()
            except Exception:
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

        except Exception as e:

            logger.error(
                "Name mute error: %s",
                e,
            )

        return

    # --------------------------------------------------------
    # MESSAGE TEXT
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
    # BIO
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
        except Exception:
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
            f"👤 <b>{user.first_name}</b>\n\n"
            "🚫 Links/usernames are not allowed "
            "in your bio or messages.\n\n"
            f"⚠️ <b>ᴡᴀʀɴɪɴɢ:</b> {count}/3\n\n"
            "🔇 3 warnings = automatic mute."
        )

        try:

            await context.bot.send_message(
                chat.id,
                text,
                parse_mode="HTML",
            )

        except Exception:
            pass

        try:

            await context.bot.send_message(
                user.id,
                text,
                parse_mode="HTML",
            )

        except Exception:
            pass

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

    if not user:
        return

    save_user(user)

    if chat.type in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        save_group(chat)

    # --------------------------------------------------------
    # CHANNEL JOIN CHECK
    # --------------------------------------------------------

    if not await is_subscribed(
        context,
        user.id,
    ):

        channel = UPDATE_CHANNEL.lstrip("@")

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔗 ᴊᴏɪɴ ᴜᴘᴅᴀᴛᴇ ᴄʜᴀɴɴᴇʟ",
                        url=f"https://t.me/{channel}",
                    )
                ]
            ]
        )

        return await update.message.reply_text(
            "🚫 <b>ᴀᴄᴄᴇss ʀᴇsᴛʀɪᴄᴛᴇᴅ</b>\n\n"
            "Please join our update channel first.\n\n"
            "After joining, press /start again.",
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    bot_username = await get_bot_username(
        context
    )

    # --------------------------------------------------------
    # START MESSAGE
    # --------------------------------------------------------

    caption = (
        "⚔️ <b>ʙɪᴏɢᴜᴀʀᴅ</b>\n\n"
        "🛡 <b>ᴛᴇʟᴇɢʀᴀᴍ ɢʀᴏᴜᴘ ᴘʀᴏᴛᴇᴄᴛɪᴏɴ</b>\n\n"
        "🚫 ʟɪɴᴋ & ᴜsᴇʀɴᴀᴍᴇ ᴘʀᴏᴛᴇᴄᴛɪᴏɴ\n"
        "🔎 ᴜsᴇʀ ʙɪᴏ ᴄʜᴇᴄᴋ\n"
        "⚠️ ᴀᴜᴛᴏ ᴡᴀʀɴɪɴɢ sʏsᴛᴇᴍ\n"
        "🔇 ᴀᴜᴛᴏ ᴍᴜᴛᴇ sʏsᴛᴇᴍ\n"
        "🛡 ᴀᴅᴍɪɴ ᴇxᴇᴍᴘᴛɪᴏɴ\n"
        "✨ ᴘᴇʀsɪsᴛᴇɴᴛ ᴍᴏɴɪᴛᴏʀɪɴɢ\n\n"
        "💫 <i>ᴋᴇᴇᴘ ʏᴏᴜʀ ɢʀᴏᴜᴘ ᴄʟᴇᴀɴ & sᴀғᴇ.</i>"
    )

    markup = start_keyboard(
        bot_username
    )

    # --------------------------------------------------------
    # START IMAGE
    # --------------------------------------------------------

    try:

        with open(
            "start.jpg",
            "rb",
        ) as photo:

            await context.bot.send_photo(
                chat_id=chat.id,
                photo=photo,
                caption=caption,
                parse_mode="HTML",
                reply_markup=markup,
            )

    except Exception as e:

        logger.warning(
            "Start image not found: %s",
            e,
        )

        await update.message.reply_text(
            caption,
            parse_mode="HTML",
            reply_markup=markup,
        )


# ============================================================
# HELP
# ============================================================


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = (
        "⚔️ <b>ʙɪᴏɢᴜᴀʀᴅ ʜᴇʟᴘ</b>\n\n"
        "🛡 <b>ᴘʀᴏᴛᴇᴄᴛɪᴏɴ</b>\n"
        "• ʙɪᴏ ʟɪɴᴋ ᴄʜᴇᴄᴋ\n"
        "• ᴍᴇssᴀɢᴇ ʟɪɴᴋ ᴄʜᴇᴄᴋ\n"
        "• ᴜsᴇʀɴᴀᴍᴇ ᴄʜᴇᴄᴋ\n"
        "• ɴᴀᴍᴇ ᴄʜᴇᴄᴋ\n"
        "• ᴀᴜᴛᴏ ᴅᴇʟᴇᴛᴇ\n"
        "• ᴡᴀʀɴɪɴɢ sʏsᴛᴇᴍ\n"
        "• ᴀᴜᴛᴏ ᴍᴜᴛᴇ\n\n"
        "👑 <b>ᴏᴡɴᴇʀ ᴄᴏᴍᴍᴀɴᴅs</b>\n"
        "<code>/setmute 2</code>\n"
        "<code>/status</code>\n"
        "<code>/broadcast</code>\n"
        "<code>/free</code>\n"
        "<code>/unfree</code>\n"
        "<code>/freelist</code>\n\n"
        "👮 <b>ᴀᴅᴍɪɴ ᴄᴏᴍᴍᴀɴᴅs</b>\n"
        "<code>/unmute</code> — Reply to user\n\n"
        "🤖 <b>ᴜsᴇʀ</b>\n"
        "<code>/start</code>\n"
        "<code>/help</code>"
    )

    if update.callback_query:

        await update.callback_query.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=back_keyboard(
                await get_bot_username(context)
            ),
        )

    else:

        await update.message.reply_text(
            text,
            parse_mode="HTML",
        )


# ============================================================
# CALLBACK HANDLER
# ============================================================


async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    if query.data == "help":

        await help_command(
            update,
            context,
        )

        return

    if query.data == "start":

        bot_username = await get_bot_username(
            context
        )

        text = (
            "⚔️ <b>ʙɪᴏɢᴜᴀʀᴅ</b>\n\n"
            "🛡 <b>ᴛᴇʟᴇɢʀᴀᴍ ɢʀᴏᴜᴘ ᴘʀᴏᴛᴇᴄᴛɪᴏɴ</b>\n\n"
            "🚫 ʟɪɴᴋ ᴘʀᴏᴛᴇᴄᴛɪᴏɴ\n"
            "🔎 ʙɪᴏ ᴘʀᴏᴛᴇᴄᴛɪᴏɴ\n"
            "⚠️ ᴡᴀʀɴɪɴɢ sʏsᴛᴇᴍ\n"
            "🔇 ᴀᴜᴛᴏ ᴍᴜᴛᴇ\n\n"
            "✨ <i>ᴄʟᴇᴀɴ • sᴀғᴇ • sᴇᴄᴜʀᴇ</i>"
        )

        await query.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=start_keyboard(
                bot_username
            ),
        )


# ============================================================
# SET MUTE
# ============================================================


async def set_mute(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if update.effective_user.id != OWNER_ID:

        return await update.message.reply_text(
            "🚫 <b>Owner only.</b>",
            parse_mode="HTML",
        )

    if (
        len(context.args) != 1
        or not context.args[0].isdigit()
    ):

        return await update.message.reply_text(
            "❌ <b>Usage:</b>\n"
            "<code>/setmute 2</code>",
            parse_mode="HTML",
        )

    hours = int(
        context.args[0]
    )

    if hours < 2 or hours > 72:

        return await update.message.reply_text(
            "⚠️ Duration must be between "
            "<b>2–72 hours</b>.",
            parse_mode="HTML",
        )

    set_mute_duration(
        hours
    )

    await update.message.reply_text(
        "✅ <b>Mute duration updated.</b>\n\n"
        f"⏱ <b>{hours} hour(s)</b>",
        parse_mode="HTML",
    )


# ============================================================
# FREE USER
# ============================================================


async def free_user(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if update.effective_user.id != OWNER_ID:
        return

    if not update.message.reply_to_message:

        return await update.message.reply_text(
            "⚠️ Reply to a user's message and use /free."
        )

    user = (
        update.message.reply_to_message.from_user
    )

    add_free_user(user)

    reset_warning(
        update.effective_chat.id,
        user.id,
    )

    await update.message.reply_text(
        f"✅ <b>{user.first_name}</b> is now free "
        "from BioGuard protection.",
        parse_mode="HTML",
    )


# ============================================================
# UNFREE USER
# ============================================================


async def unfree_user(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if update.effective_user.id != OWNER_ID:
        return

    if not update.message.reply_to_message:

        return await update.message.reply_text(
            "⚠️ Reply to a user's message and use /unfree."
        )

    user = (
        update.message.reply_to_message.from_user
    )

    remove_free_user(
        user.id
    )

    await update.message.reply_text(
        f"🔓 <b>{user.first_name}</b> has been removed "
        "from the free list.",
        parse_mode="HTML",
    )


# ============================================================
# FREE LIST
# ============================================================


async def free_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if update.effective_user.id != OWNER_ID:
        return

    users = list(
        free_users_collection.find({})
    )

    if not users:

        return await update.message.reply_text(
            "📋 Free list is empty."
        )

    text = "📋 <b>Free Users</b>\n\n"

    for index, user in enumerate(
        users,
        start=1,
    ):

        name = user.get(
            "first_name",
            "Unknown",
        )

        user_id = user.get(
            "user_id",
            user.get("_id"),
        )

        text += (
            f"{index}. {name} — "
            f"<code>{user_id}</code>\n"
        )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
    )


# ============================================================
# UNMUTE
# ============================================================


async def unmute(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if update.effective_chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        return

    if not await is_admin(
        context,
        update.effective_chat.id,
        update.effective_user.id,
    ):

        return await update.message.reply_text(
            "🚫 Admins only."
        )

    if not update.message.reply_to_message:

        return await update.message.reply_text(
            "⚠️ Reply to a user's message and use /unmute."
        )

    target = (
        update.message.reply_to_message.from_user
    )

    try:

        await unmute_user(
            context,
            update.effective_chat.id,
            target.id,
        )

        reset_warning(
            update.effective_chat.id,
            target.id,
        )

        await update.message.reply_text(
            f"🔓 <b>{target.first_name}</b> has been unmuted.",
            parse_mode="HTML",
        )

    except Exception as e:

        logger.error(
            "Unmute error: %s",
            e,
        )

        await update.message.reply_text(
            "❌ Unable to unmute this user."
        )


# ============================================================
# STATUS
# ============================================================


async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if update.effective_user.id != OWNER_ID:
        return

    users = users_collection.count_documents({})
    groups = groups_collection.count_documents({})
    free = free_users_collection.count_documents({})

    duration = get_mute_duration()

    text = (
        "📊 <b>ʙɪᴏɢᴜᴀʀᴅ sᴛᴀᴛᴜs</b>\n\n"
        f"👤 Users: <b>{users}</b>\n"
        f"👥 Groups: <b>{groups}</b>\n"
        f"🆓 Free Users: <b>{free}</b>\n"
        f"🔇 Mute Duration: <b>{duration}h</b>"
    )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
    )


# ============================================================
# BROADCAST
# ============================================================


async def broadcast(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if update.effective_user.id != OWNER_ID:
        return

    if not update.message.reply_to_message:

        return await update.message.reply_text(
            "⚠️ Reply to a message and use /broadcast."
        )

    source = (
        update.message.reply_to_message
    )

    users = [
        item["_id"]
        for item in users_collection.find(
            {},
            {"_id": 1},
        )
    ]

    groups = [
        item["_id"]
        for item in groups_collection.find(
            {},
            {"_id": 1},
        )
    ]

    targets = list(
        dict.fromkeys(
            users + groups
        )
    )

    success = 0
    failed = 0

    status = await update.message.reply_text(
        "📢 <b>Broadcast started...</b>\n\n"
        "⏳ Sending message...",
        parse_mode="HTML",
    )

    for target_id in targets:

        try:

            await source.copy(
                chat_id=target_id
            )

            success += 1

        except Exception as e:

            failed += 1

            error = str(e).lower()

            if any(
                item in error
                for item in (
                    "blocked",
                    "deactivated",
                    "chat not found",
                    "user not found",
                )
            ):

                users_collection.delete_one(
                    {"_id": target_id}
                )

                groups_collection.delete_one(
                    {"_id": target_id}
                )

        await asyncio.sleep(
            0.08
        )

    await status.edit_text(
        "📢 <b>ʙʀᴏᴀᴅᴄᴀsᴛ ᴄᴏᴍᴘʟᴇᴛᴇ</b>\n\n"
        f"✅ Success: <b>{success}</b>\n"
        f"❌ Failed: <b>{failed}</b>\n"
        f"📊 Total: <b>{len(targets)}</b>",
        parse_mode="HTML",
    )


# ============================================================
# ERROR HANDLER
# ============================================================


async def error_handler(
    update,
    context: ContextTypes.DEFAULT_TYPE,
):

    logger.error(
        "Unhandled exception:",
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================


def main():

    logger.info(
        "⚔️ Starting BioGuard..."
    )

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    # Commands
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
            "setmute",
            set_mute,
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
            "broadcast",
            broadcast,
        )
    )

    application.add_handler(
        CommandHandler(
            "unmute",
            unmute,
        )
    )

    application.add_handler(
        CommandHandler(
            "free",
            free_user,
        )
    )

    application.add_handler(
        CommandHandler(
            "unfree",
            unfree_user,
        )
    )

    application.add_handler(
        CommandHandler(
            "freelist",
            free_list,
        )
    )

    # Callback buttons
    application.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    # Group moderation
    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & ~filters.COMMAND,
            check_message,
        )
    )

    # Error handler
    application.add_error_handler(
        error_handler
    )

    logger.info(
        "✅ BioGuard is running."
    )

    application.run_polling(
        drop_pending_updates=True
    )


# ============================================================
# START BOT
# ============================================================

if __name__ == "__main__":
    main()
