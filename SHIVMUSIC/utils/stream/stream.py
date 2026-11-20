import asyncio
import logging
import random
from typing import Union

from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardMarkup

import config
from SHIVMUSIC import Carbon, YouTube, app
from SHIVMUSIC.core.call import ANJALI
from SHIVMUSIC.misc import db
from SHIVMUSIC.utils.cards import send_card, send_track_card, start_thumb
from SHIVMUSIC.utils.database import add_active_video_chat, is_active_chat, is_autoplay_on
from SHIVMUSIC.utils.exceptions import AssistantErr
from SHIVMUSIC.utils.html_builder import build_play_card, build_queue_card
from SHIVMUSIC.utils.inline import aq_markup, close_markup, stream_markup
from SHIVMUSIC.utils.pastebin import ANJALIBin
from SHIVMUSIC.utils.prefetch import prefetch_next
from SHIVMUSIC.utils.stream.queue import put_queue, put_queue_index

LOGGER = logging.getLogger(__name__)


# kept for old imports (callback.py / other plugins may still import it)
def audio_status_html(chat_id):
    from SHIVMUSIC.utils.html_builder import _audio_rows, quote

    rows = _audio_rows(chat_id)
    return "\n" + quote(f"<b>✦ AUDIO SETTINGS ✦</b>\n{rows}", expandable=True) if rows else ""


def get_random_img(img_list):
    if img_list:
        if isinstance(img_list, list):
            return random.choice(img_list)
        return img_list
    return "https://files.catbox.moe/6r97s4.jpg"


async def safe_delete(message):
    if not message:
        return
    try:
        await message.delete()
    except Exception:
        pass


async def _store_card(chat_id, run, markup_kind):
    if run and db.get(chat_id) and isinstance(db[chat_id], list) and len(db[chat_id]) > 0:
        db[chat_id][0]["mystic"] = run
        db[chat_id][0]["markup"] = markup_kind


async def _send_queue_card(_, chat_id, original_chat_id, position, title, duration, user_name):
    await send_card(
        app,
        original_chat_id,
        build_queue_card(position, title, duration, user_name, chat_id),
        InlineKeyboardMarkup(aq_markup(_, chat_id)),
        photo=None,
    )


async def _now_playing_card(
    _, chat_id, original_chat_id, vidid, user_id, title, duration, user_name,
    link, source="YouTube", static_img=None, markup_kind="stream",
):
    caption = build_play_card(link, title, duration, user_name, chat_id, await is_autoplay_on(chat_id), source)
    run = await send_track_card(
        app, chat_id, original_chat_id, vidid, user_id, caption,
        InlineKeyboardMarkup(stream_markup(_, chat_id)), static_img=static_img,
    )
    await _store_card(chat_id, run, markup_kind)
    return run


async def stream(
    _, mystic, user_id, result, chat_id, user_name, original_chat_id,
    video: Union[bool, str] = None, streamtype: Union[bool, str] = None,
    spotify: Union[bool, str] = None, forceplay: Union[bool, str] = None,
):
    if not result:
        return
    if forceplay:
        await ANJALI.force_stop_stream(chat_id)

    # ------------------------------------------------------------ playlist
    if streamtype == "playlist":
        msg = f"{_['play_19']}\n\n"
        count = 0
        position = 0
        for search in result:
            if int(count) >= config.PLAYLIST_FETCH_LIMIT:
                break
            try:
                title, duration_min, duration_sec, thumbnail, vidid = await YouTube.details(
                    search, False if spotify else True
                )
            except Exception:
                continue
            if not vidid or str(duration_min) == "None" or (duration_sec or 0) > config.DURATION_LIMIT:
                continue

            if await is_active_chat(chat_id):
                await put_queue(chat_id, original_chat_id, f"vid_{vidid}", title, duration_min, user_name, vidid, user_id, "video" if video else "audio")
                position = len(db.get(chat_id)) - 1
                count += 1
                msg += f"{count}. {title[:70]}\n{_['play_20']} {position}\n\n"
            else:
                if not forceplay:
                    db[chat_id] = []
                status = True if video else None
                file_path, direct = await YouTube.download(vidid, mystic, video=status, videoid=True)
                if not file_path or str(file_path) == "None":
                    raise AssistantErr(_["play_14"])
                asyncio.create_task(safe_delete(mystic))
                await ANJALI.join_call(chat_id, original_chat_id, file_path, video=status, image=thumbnail)
                await put_queue(chat_id, original_chat_id, file_path, title, duration_min, user_name, vidid, user_id, "video" if video else "audio", forceplay=forceplay)
                await _now_playing_card(
                    _, chat_id, original_chat_id, vidid, user_id, title, duration_min, user_name,
                    f"https://t.me/{app.username}?start=info_{vidid}",
                )
                await ANJALI.on_track_started(chat_id)

        if count == 0:
            return
        prefetch_next(chat_id)
        asyncio.create_task(safe_delete(mystic))
        link = await ANJALIBin(msg)
        try:
            carbon = await Carbon.generate(msg, random.randint(100, 10000000))
            return await app.send_photo(
                original_chat_id, photo=carbon, caption=_["play_21"].format(position, link),
                reply_markup=close_markup(_), has_spoiler=False,
            )
        except Exception:
            return await app.send_message(
                original_chat_id, _["play_21"].format(position, link),
                reply_markup=close_markup(_), disable_web_page_preview=True,
            )

    # ------------------------------------------------------------ youtube
    elif streamtype == "youtube":
        link, vidid, title, duration_min, thumbnail = (
            result["link"], result["vidid"], (result["title"]).title(), result["duration_min"], result["thumb"],
        )
        status = True if video else None

        if await is_active_chat(chat_id):
            # lazy queue: downloads while the current song is still playing (prefetch) -> instant add
            await put_queue(chat_id, original_chat_id, f"vid_{vidid}", title, duration_min, user_name, vidid, user_id, "video" if video else "audio")
            position = len(db.get(chat_id)) - 1
            asyncio.create_task(safe_delete(mystic))
            await _send_queue_card(_, chat_id, original_chat_id, position, title, duration_min, user_name)
            prefetch_next(chat_id)
        else:
            # thumbnail renders in a worker thread WHILE the song downloads
            start_thumb(vidid, user_id, title, duration_min)
            file_path, direct = await YouTube.download(vidid, mystic, videoid=True, video=status)
            if not file_path or str(file_path) == "None":
                raise AssistantErr(_["play_14"])
            asyncio.create_task(safe_delete(mystic))
            if not forceplay:
                db[chat_id] = []
            await ANJALI.join_call(chat_id, original_chat_id, file_path, video=status, image=thumbnail)
            await put_queue(chat_id, original_chat_id, file_path, title, duration_min, user_name, vidid, user_id, "video" if video else "audio", forceplay=forceplay)
            await _now_playing_card(
                _, chat_id, original_chat_id, vidid, user_id, title, duration_min, user_name,
                f"https://t.me/{app.username}?start=info_{vidid}",
            )
            await ANJALI.on_track_started(chat_id)

    # ------------------------------------------------------------ live
    elif streamtype == "live":
        link, vidid, title, thumbnail = result["link"], result["vidid"], (result["title"]).title(), result["thumb"]
        status = True if video else None
        asyncio.create_task(safe_delete(mystic))

        if await is_active_chat(chat_id):
            await put_queue(chat_id, original_chat_id, f"live_{vidid}", title, "Live", user_name, vidid, user_id, "video" if video else "audio")
            position = len(db.get(chat_id)) - 1
            await _send_queue_card(_, chat_id, original_chat_id, position, title, "LIVE", user_name)
        else:
            if not forceplay:
                db[chat_id] = []
            n, file_path = await YouTube.video(link)
            if n == 0 or not file_path or str(file_path) == "None":
                raise AssistantErr(_["str_3"])
            await ANJALI.join_call(chat_id, original_chat_id, file_path, video=status, image=thumbnail)
            await put_queue(chat_id, original_chat_id, f"live_{vidid}", title, "Live", user_name, vidid, user_id, "video" if video else "audio", forceplay=forceplay)
            await _now_playing_card(
                _, chat_id, original_chat_id, vidid, user_id, title, "LIVE", user_name,
                f"https://t.me/{app.username}?start=info_{vidid}", "YouTube Live", markup_kind="tg",
            )
            await ANJALI.on_track_started(chat_id)

    # ------------------------------------------------------------ soundcloud
    elif streamtype == "soundcloud":
        file_path, title, duration_min = result["filepath"], result["title"], result["duration_min"]
        asyncio.create_task(safe_delete(mystic))

        if await is_active_chat(chat_id):
            await put_queue(chat_id, original_chat_id, file_path, title, duration_min, user_name, streamtype, user_id, "audio")
            position = len(db.get(chat_id)) - 1
            await _send_queue_card(_, chat_id, original_chat_id, position, title, duration_min, user_name)
        else:
            if not forceplay:
                db[chat_id] = []
            await ANJALI.join_call(chat_id, original_chat_id, file_path, video=None)
            await put_queue(chat_id, original_chat_id, file_path, title, duration_min, user_name, streamtype, user_id, "audio", forceplay=forceplay)
            await _now_playing_card(
                _, chat_id, original_chat_id, streamtype, user_id, title, duration_min, user_name,
                config.SUPPORT_CHAT, "SoundCloud", static_img=config.SOUNCLOUD_IMG_URL, markup_kind="tg",
            )
            await ANJALI.on_track_started(chat_id)

    # ------------------------------------------------------------ telegram file
    elif streamtype == "telegram":
        file_path, link, title, duration_min = result["path"], result["link"], (result["title"]).title(), result["dur"]
        status = True if video else None
        asyncio.create_task(safe_delete(mystic))

        if await is_active_chat(chat_id):
            await put_queue(chat_id, original_chat_id, file_path, title, duration_min, user_name, streamtype, user_id, "video" if video else "audio")
            position = len(db.get(chat_id)) - 1
            await _send_queue_card(_, chat_id, original_chat_id, position, title, duration_min, user_name)
        else:
            if not forceplay:
                db[chat_id] = []
            await ANJALI.join_call(chat_id, original_chat_id, file_path, video=status)
            await put_queue(chat_id, original_chat_id, file_path, title, duration_min, user_name, streamtype, user_id, "video" if video else "audio", forceplay=forceplay)
            if video:
                await add_active_video_chat(chat_id)
            await _now_playing_card(
                _, chat_id, original_chat_id, streamtype, user_id, title, duration_min, user_name,
                link, "Telegram", static_img=(config.TELEGRAM_VIDEO_URL if video else config.TELEGRAM_AUDIO_URL),
                markup_kind="tg",
            )
            await ANJALI.on_track_started(chat_id)

    # ------------------------------------------------------------ index / m3u8
    elif streamtype == "index":
        link, title, duration_min = result, "Index or M3u8 Link", "00:00"
        asyncio.create_task(safe_delete(mystic))

        if await is_active_chat(chat_id):
            await put_queue_index(chat_id, original_chat_id, "index_url", title, duration_min, user_name, link, "video" if video else "audio")
            position = len(db.get(chat_id)) - 1
            await _send_queue_card(_, chat_id, original_chat_id, position, title, duration_min, user_name)
        else:
            if not forceplay:
                db[chat_id] = []
            await ANJALI.join_call(chat_id, original_chat_id, link, video=True if video else None)
            await put_queue_index(chat_id, original_chat_id, "index_url", title, duration_min, user_name, link, "video" if video else "audio", forceplay=forceplay)
            await _now_playing_card(
                _, chat_id, original_chat_id, "index", user_id, title, "LIVE", user_name,
                None, "Stream Link", static_img=config.STREAM_IMG_URL, markup_kind="tg",
            )
            await ANJALI.on_track_started(chat_id)
