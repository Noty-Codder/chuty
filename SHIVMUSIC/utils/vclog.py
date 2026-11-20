"""VC logger: sends voice-chat events to LOGGER_ID when the sudo switch is ON (/vclogger enable)."""
import logging

from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
from SHIVMUSIC import app
from SHIVMUSIC.utils.database import is_vclog_on
from SHIVMUSIC.utils.html_builder import build_vc_log, clip, esc

LOGGER = logging.getLogger("VCLOG")
_chat_cache: dict = {}


async def _chat_info(chat_id: int):
    if chat_id in _chat_cache:
        return _chat_cache[chat_id]
    title, link = "Unknown Chat", None
    try:
        chat = await app.get_chat(chat_id)
        title = chat.title or title
        if chat.username:
            link = f"https://t.me/{chat.username}"
    except Exception:
        pass
    _chat_cache[chat_id] = (title, link)
    return title, link


async def vc_log(event: str, chat_id: int, track: str = None, by: str = None, extra: str = None):
    """Never raises. Safe to call from anywhere."""
    try:
        if not config.LOGGER_ID or not await is_vclog_on():
            return
        if int(chat_id) == int(config.LOGGER_ID):
            return
        title, link = await _chat_info(int(chat_id))
        lines = []
        if track:
            lines.append(f"<b>TRACK</b>  │  <b>{esc(clip(track, 45))}</b>")
        if by:
            lines.append(f"<b>BY</b>  │  <b>{esc(clip(by, 28))}</b>")
        if extra:
            lines.append(f"<b>INFO</b>  │  <i>{esc(extra)}</i>")
        text = build_vc_log(event, title, chat_id, app.mention, "\n".join(lines), link)
        buttons = []
        if link:
            buttons.append([InlineKeyboardButton("ɢʀᴏᴜᴘ ʟɪɴᴋ", url=link)])
        await app.send_message(
            config.LOGGER_ID,
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
        )
    except Exception as e:
        LOGGER.debug("vc_log failed: %s", e)
