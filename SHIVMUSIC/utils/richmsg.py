"""
Native Telegram RICH MESSAGE (real table) — powered by FTMGram.

Library split in this project
  * kurigram (module `pyrogram`) : the whole bot, plugins, assistants, py-tgcalls  (unchanged, stable)
  * ftmgram  (module `ftmgram`)  : ONLY rich messages (tables / expandable quotes), via a tiny
                                   second bot session that never receives updates.

Everything is wrapped: on any failure send_rich_welcome() returns False and the caller falls
back to the HTML blockquote card, so /start can never break.
"""
import asyncio
import logging

import config

LOGGER = logging.getLogger("RICHMSG")

HEAD = ["FEATURE", "DETAILS"]
ROWS = [
    ["🎵 Streaming", "High-quality, zero lag"],
    ["🚀 Speed", "Instant response, always on"],
    ["🎚 Equalizer", "13 pro presets + volume"],
    ["🔁 Autoplay", "Spotify-style endless radio"],
    ["👑 VIP Care", "Priority support, smooth vibes"],
]

_client = None
_lock = asyncio.Lock()
_disabled = False


async def _get_client():
    """Start the ftmgram helper session once (lazy)."""
    global _client, _disabled
    if _client is not None:
        return _client
    async with _lock:
        if _client is not None:
            return _client
        from ftmgram import Client as FtmClient

        c = FtmClient(
            name="SHIVRICH",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            bot_token=config.BOT_TOKEN,
            in_memory=True,
            no_updates=True,
        )
        await c.start()
        _client = c
        return c


def _convert_markup(markup):
    """kurigram InlineKeyboardMarkup -> ftmgram InlineKeyboardMarkup."""
    if markup is None:
        return None
    from ftmgram.types import InlineKeyboardButton as FB
    from ftmgram.types import InlineKeyboardMarkup as FM

    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for b in row:
            data = {"text": b.text}
            for key in ("url", "callback_data", "user_id"):
                if getattr(b, key, None):
                    data[key] = getattr(b, key)
            full = dict(data)
            if getattr(b, "icon_custom_emoji_id", None):
                full["icon_custom_emoji_id"] = b.icon_custom_emoji_id
            if getattr(b, "style", None) is not None:
                full["style"] = getattr(b.style, "name", b.style)
            try:
                new_row.append(FB(**full))
            except Exception:
                new_row.append(FB(**data))  # plain button if colours/emoji unsupported
        rows.append(new_row)
    return FM(rows)


def _add_table(b, rows, head=HEAD):
    table = getattr(b, "table")
    attempts = (
        lambda: table(rows, headers=head, bordered=True),
        lambda: table(rows, headers=head),
        lambda: table(head, rows),
        lambda: table([head] + rows),
    )
    last = None
    for attempt in attempts:
        try:
            res = attempt()
            return res if res is not None else b
        except TypeError as e:
            last = e
    raise last


class CardRef:
    """Light handle for a rich-message card (so the timer / buttons code keeps working with kurigram)."""

    def __init__(self, chat_id, message_id):
        self.id = message_id
        self.chat_id = chat_id
        self.chat = type("C", (), {"id": chat_id})()

    async def edit_reply_markup(self, reply_markup=None):
        from SHIVMUSIC import app

        return await app.edit_message_reply_markup(self.chat_id, self.id, reply_markup)

    async def delete(self):
        from SHIVMUSIC import app

        return await app.delete_messages(self.chat_id, self.id)


async def send_rich_card(chat_id: int, title: str, rows: list, reply_markup=None):
    """Send a native TABLE card (FEATURE | DETAILS). Returns CardRef or None."""
    global _disabled
    if _disabled:
        return None
    try:
        client = await _get_client()
        if not hasattr(client, "send_rich_message"):
            _disabled = True
            return None
        from ftmgram.helpers import RichMessageBuilder

        b = RichMessageBuilder()
        b = b.title(title) or b
        b = _add_table(b, rows)
        msg = await client.send_rich_message(chat_id, b.build(), reply_markup=_convert_markup(reply_markup))
        return CardRef(chat_id, msg.id)
    except ImportError:
        _disabled = True
        return None
    except Exception as e:
        LOGGER.warning("rich card unavailable (%s) -> HTML card", str(e)[:140])
        return None


def _build(user_name: str, bot_name: str):
    from ftmgram.helpers import RichMessageBuilder

    b = RichMessageBuilder()
    b = b.title(f"HEY {user_name.upper()}, WELCOME ABOARD! 🎶") or b
    b = b.paragraph(f"I AM {bot_name.upper()} — YOUR PERSONAL MUSIC COMPANION, LIVE 24/7. 🎧") or b
    table = getattr(b, "table")
    attempts = (
        lambda: table(ROWS, headers=HEAD, bordered=True),
        lambda: table(ROWS, headers=HEAD),
        lambda: table(HEAD, ROWS),
        lambda: table([HEAD] + ROWS),
    )
    last = None
    for attempt in attempts:
        try:
            res = attempt()
            b = res if res is not None else b
            last = None
            break
        except TypeError as e:
            last = e
    if last is not None:
        raise last
    b = b.paragraph("✦ WHY CHOOSE ME? USER-FRIENDLY COMMANDS • CRYSTAL-CLEAR AUDIO • TRUSTED BY THOUSANDS OF GROUPS.") or b
    b = b.paragraph("INVITE ME NOW AND ENJOY THE VIBE! 🎉") or b
    return b.build()


async def send_rich_welcome(_unused_client, chat_id: int, user_name: str, bot_name: str, reply_markup=None) -> bool:
    global _disabled
    if _disabled:
        return False
    try:
        client = await _get_client()
        if not hasattr(client, "send_rich_message"):
            _disabled = True
            return False
        rich = _build(user_name, bot_name)
        await client.send_rich_message(chat_id, rich, reply_markup=_convert_markup(reply_markup))
        return True
    except ImportError:
        _disabled = True  # ftmgram not installed -> never try again
        return False
    except Exception as e:
        LOGGER.warning("rich welcome unavailable (%s) -> HTML fallback", str(e)[:140])
        return False


async def stop_rich_client():
    global _client
    if _client is not None:
        try:
            await _client.stop()
        except Exception:
            pass
        _client = None
