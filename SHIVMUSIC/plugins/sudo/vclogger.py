# VC LOGGER  —  /vclogger enable | disable | status   (sudo only)
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from SHIVMUSIC import app
from SHIVMUSIC.misc import SUDOERS
from SHIVMUSIC.utils.database import VC_LOG_FLAG, add_off, add_on, is_vclog_on
from SHIVMUSIC.utils.html_builder import em, quote
from SHIVMUSIC.utils.vclog import vc_log
from config import BANNED_USERS


@app.on_message(filters.command(["vclogger", "vclog"]) & SUDOERS)
async def vclogger_cmd(client, message: Message):
    if len(message.command) != 2 or message.command[1].lower() not in ("enable", "disable", "on", "off", "status"):
        state = "ON 🟢" if await is_vclog_on() else "OFF 🔴"
        return await message.reply_text(
            quote(f"{em('headset')} <b>VC LOGGER</b>  │  <b>{state}</b>\n<b>USAGE</b>  │  <code>/vclogger enable|disable|status</code>"),
            parse_mode=ParseMode.HTML,
        )
    arg = message.command[1].lower()
    if arg in ("enable", "on"):
        await add_on(VC_LOG_FLAG)
        text = f"{em('ok')} <b>VC LOGGER ENABLED — every VC start / end / track will be logged.</b>"
    elif arg in ("disable", "off"):
        await add_off(VC_LOG_FLAG)
        text = f"{em('bad')} <b>VC LOGGER DISABLED.</b>"
    else:
        text = f"{em('headset')} <b>VC LOGGER IS {'ON 🟢' if await is_vclog_on() else 'OFF 🔴'}</b>"
    await message.reply_text(quote(text), parse_mode=ParseMode.HTML)


# ---- native Telegram voice-chat service messages ----
@app.on_message(filters.video_chat_started & filters.group, group=40)
async def _vc_started(_, message: Message):
    await vc_log("VOICE CHAT STARTED", message.chat.id, by=getattr(message.from_user, "first_name", None))


@app.on_message(filters.video_chat_ended & filters.group, group=41)
async def _vc_ended(_, message: Message):
    await vc_log("VOICE CHAT ENDED", message.chat.id, by=getattr(message.from_user, "first_name", None))
