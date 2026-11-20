"""
One place that SENDS the now-playing / autoplay / queue cards.

* ONE message per card (photo + caption, or plain text when thumbnails are OFF)
* thumbnail is generated in the background (thread) -> bot never freezes
* `start_thumb()` can be called BEFORE the download starts, so the thumbnail is
  ready by the time the song is
* photo failure -> automatic text fallback (card is never lost)
"""
import asyncio
import logging
import random
from typing import Dict, Optional, Tuple

from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardMarkup

import config
from SHIVMUSIC import app
from SHIVMUSIC.utils.database import is_thumb_on

LOGGER = logging.getLogger("CARDS")
FALLBACK_IMG = "https://files.catbox.moe/6r97s4.jpg"
THUMB_TIMEOUT = 7  # seconds we are willing to wait for the custom thumbnail

# (vidid, user_id) -> running thumbnail task
_thumb_tasks: Dict[Tuple[str, int], "asyncio.Task"] = {}


def pick_img(img_list) -> str:
    if isinstance(img_list, (list, tuple)) and img_list:
        return random.choice(list(img_list))
    if isinstance(img_list, str) and img_list:
        return img_list
    return FALLBACK_IMG


def _yt_thumb(vidid) -> Optional[str]:
    vid = str(vidid or "")
    if len(vid) == 11:
        return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    return None


def start_thumb(vidid, user_id=0, title=None, duration=None, channel=None):
    """Kick off thumbnail generation in the background (call as early as possible)."""
    vid = str(vidid or "")
    if len(vid) != 11:
        return
    key = (vid, int(user_id or 0))
    old = _thumb_tasks.get(key)
    if old and not old.done():
        return

    async def _run():
        from SHIVMUSIC.utils.thumbnails import get_thumb

        return await get_thumb(vid, int(user_id or 0), None, title=title, duration=duration, channel=channel)

    try:
        task = asyncio.create_task(_run())
    except RuntimeError:
        return
    _thumb_tasks[key] = task
    # forget the finished task after a while (the image itself stays cached on disk)
    task.add_done_callback(
        lambda t, k=key: asyncio.get_running_loop().call_later(180, _thumb_tasks.pop, k, None)
        if not t.cancelled() and t.exception() is None
        else _thumb_tasks.pop(k, None)
    )


async def resolve_thumb(chat_id: int, vidid: str, user_id, client, static_img=None) -> Optional[str]:
    """Return an image for the card, or None when thumbnails are switched off."""
    if not await is_thumb_on(chat_id):
        return None
    if static_img:
        return pick_img(static_img)

    key = (str(vidid), int(user_id or 0))
    try:
        if key not in _thumb_tasks:
            start_thumb(vidid, user_id)
        task = _thumb_tasks.get(key)
        if task is not None:
            img = await asyncio.wait_for(asyncio.shield(task), timeout=THUMB_TIMEOUT)
            if img:
                return img
    except Exception as e:  # thumbnail generation must never break playback
        LOGGER.warning("thumbnail failed for %s: %s", vidid, str(e)[:120])
    return _yt_thumb(vidid) or pick_img(getattr(config, "PLAYLIST_IMG_URL", None))


async def send_card(
    client,
    chat_id: int,
    caption: str,
    markup=None,
    photo: Optional[str] = None,
):
    """Send a card. `photo=None` => text card (thumbnail OFF or unavailable)."""
    client = client or app
    if markup is not None and not isinstance(markup, InlineKeyboardMarkup):
        markup = InlineKeyboardMarkup(markup)

    if photo:
        try:
            return await client.send_photo(
                chat_id, photo=photo, caption=caption, reply_markup=markup, parse_mode=ParseMode.HTML
            )
        except Exception as e:
            LOGGER.warning("send_photo failed (%s) -> text card", str(e)[:120])
    try:
        return await client.send_message(
            chat_id,
            text=caption,
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        LOGGER.error("send_message card failed: %s", str(e)[:160])
    return None


async def send_track_card(
    client,
    chat_id: int,
    original_chat_id: int,
    vidid: str,
    user_id,
    caption: str,
    markup,
    static_img=None,
):
    """Resolve the thumbnail (if enabled) and send the card in one call."""
    photo = await resolve_thumb(chat_id, vidid, user_id, client, static_img)
    return await send_card(client, original_chat_id, caption, markup, photo)
