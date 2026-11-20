# ===========================================================
# ©️ 2026-27 Beta Bots  |  Developer : t.me/SUKOON_s
# Thumbnail ON / OFF   —   /thumbnail [on|off]   (alias: /thumb)
# ===========================================================
import random

from pyrogram import filters
from pyrogram.enums import ButtonStyle, ParseMode
from pyrogram.errors import MessageNotModified
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from SHIVMUSIC import app
from SHIVMUSIC.utils.chatadmin import is_chat_admin
from SHIVMUSIC.utils.database import is_thumb_on, thumb_off, thumb_on
from SHIVMUSIC.utils.html_builder import build_thumb_panel
from config import BANNED_USERS

PREMIUM_EMOJIS = [5422831825178206894, 5368324170673489600, 5206607081334906820, 5206380668048496464]


def _btn(text, data, style):
    return InlineKeyboardButton(
        text, callback_data=data, style=style, icon_custom_emoji_id=random.choice(PREMIUM_EMOJIS)
    )


def panel_buttons(chat_id: int, enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                _btn("Thumbnail ON ✓" if enabled else "Turn ON", f"TH|on|{chat_id}", ButtonStyle.SUCCESS),
                _btn("Turn OFF" if enabled else "Thumbnail OFF ✓", f"TH|off|{chat_id}", ButtonStyle.DANGER),
            ],
            [InlineKeyboardButton("✖ Close", callback_data="close")],
        ]
    )


@app.on_message(filters.command(["thumbnail", "thumb"]) & filters.group & ~BANNED_USERS)
async def thumbnail_cmd(client, message: Message):
    if not message.from_user:
        return
    chat_id = message.chat.id
    if len(message.command) > 1:
        if not await is_chat_admin(client, chat_id, message.from_user.id):
            return await message.reply_text("<b>❌ ONLY ADMINS CAN CHANGE THUMBNAIL.</b>", parse_mode=ParseMode.HTML)
        arg = message.command[1].lower()
        if arg in ("on", "enable"):
            await thumb_on(chat_id)
        elif arg in ("off", "disable"):
            await thumb_off(chat_id)
    enabled = await is_thumb_on(chat_id)
    await message.reply_text(
        build_thumb_panel(enabled),
        reply_markup=panel_buttons(chat_id, enabled),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


@app.on_callback_query(filters.regex(r"^TH\|") & ~BANNED_USERS)
async def thumbnail_cb(client, query: CallbackQuery):
    _, action, raw_chat = query.data.split("|")
    chat_id = int(raw_chat)
    if not await is_chat_admin(client, chat_id, query.from_user.id):
        return await query.answer("❌ Only admins can change this setting!", show_alert=True)
    if action == "on":
        await thumb_on(chat_id)
        note = "Thumbnail ON ✅"
    else:
        await thumb_off(chat_id)
        note = "Thumbnail OFF ❌"
    enabled = await is_thumb_on(chat_id)
    try:
        await query.message.edit_text(
            build_thumb_panel(enabled),
            reply_markup=panel_buttons(chat_id, enabled),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except MessageNotModified:
        pass
    except Exception:
        pass
    await query.answer(note)
