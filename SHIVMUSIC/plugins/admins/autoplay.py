# ===========================================================
# ©️ 2026-27 Beta Bots  |  Developer : t.me/SUKOON_s
# Autoplay control panel  —  /autoplay [on|off|similar|artist|discover]
# Works with or without an active stream.
# ===========================================================
import random

from pyrogram import filters
from pyrogram.enums import ButtonStyle, ParseMode
from pyrogram.errors import MessageNotModified
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from SHIVMUSIC import app
from SHIVMUSIC.utils.autoplay import discard_prepared
from SHIVMUSIC.utils.inline.play import set_autoplay_flag
from SHIVMUSIC.utils.chatadmin import is_chat_admin
from SHIVMUSIC.utils.database import (
    AUTOPLAY_STYLES,
    autoplay_off,
    autoplay_on,
    get_autoplay_style,
    is_autoplay_on,
    set_autoplay_style,
)
from SHIVMUSIC.utils.html_builder import build_autoplay_panel
from config import BANNED_USERS

PREMIUM_EMOJIS = [5422831825178206894, 5368324170673489600, 5206607081334906820, 5206380668048496464]


def _btn(text, data, style):
    return InlineKeyboardButton(
        text, callback_data=data, style=style, icon_custom_emoji_id=random.choice(PREMIUM_EMOJIS)
    )


def panel_buttons(chat_id: int, enabled: bool, style: str) -> InlineKeyboardMarkup:
    def mark(key):
        return ("● " if key == style else "") + key.capitalize()

    return InlineKeyboardMarkup(
        [
            [
                _btn("Enable ✓" if not enabled else "Enabled ✓", f"AP|on|{chat_id}", ButtonStyle.SUCCESS),
                _btn("Disable ✗" if enabled else "Disabled ✗", f"AP|off|{chat_id}", ButtonStyle.DANGER),
            ],
            [
                _btn(mark("similar"), f"AP|similar|{chat_id}", ButtonStyle.PRIMARY),
                _btn(mark("artist"), f"AP|artist|{chat_id}", ButtonStyle.PRIMARY),
                _btn(mark("discover"), f"AP|discover|{chat_id}", ButtonStyle.PRIMARY),
            ],
            [InlineKeyboardButton("✖ Close", callback_data="close")],
        ]
    )


async def _render(chat_id: int):
    enabled = await is_autoplay_on(chat_id)
    set_autoplay_flag(chat_id, enabled)
    style = await get_autoplay_style(chat_id)
    return build_autoplay_panel(enabled, style), panel_buttons(chat_id, enabled, style)


@app.on_message(filters.command(["autoplay"]) & filters.group & ~BANNED_USERS)
async def autoplay_mode(client, message: Message):
    chat_id = message.chat.id
    if not message.from_user:
        return
    if len(message.command) > 1:
        if not await is_chat_admin(client, chat_id, message.from_user.id):
            return await message.reply_text("<b>❌ ONLY ADMINS CAN CHANGE AUTOPLAY.</b>", parse_mode=ParseMode.HTML)
        arg = message.command[1].lower()
        if arg in ("enable", "on"):
            await autoplay_on(chat_id)
        elif arg in ("disable", "off"):
            await autoplay_off(chat_id)
            discard_prepared(chat_id)
        elif arg in AUTOPLAY_STYLES:
            await set_autoplay_style(chat_id, arg)
            await autoplay_on(chat_id)
    text, markup = await _render(chat_id)
    await message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


@app.on_callback_query(filters.regex(r"^AP\|") & ~BANNED_USERS)
async def autoplay_cb(client, query: CallbackQuery):
    _, action, raw_chat = query.data.split("|")
    chat_id = int(raw_chat)
    if not await is_chat_admin(client, chat_id, query.from_user.id):
        return await query.answer("❌ Only admins can change this setting!", show_alert=True)

    if action == "on":
        await autoplay_on(chat_id)
        note = "Autoplay enabled ✅"
    elif action == "off":
        await autoplay_off(chat_id)
        discard_prepared(chat_id)
        note = "Autoplay disabled ❌"
    elif action in AUTOPLAY_STYLES:
        await set_autoplay_style(chat_id, action)
        await autoplay_on(chat_id)
        note = f"Style: {action.capitalize()} ✅"
    else:
        return await query.answer()

    text, markup = await _render(chat_id)
    try:
        await query.message.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except MessageNotModified:
        pass
    except Exception:
        pass
    await query.answer(note)
