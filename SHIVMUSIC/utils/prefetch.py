"""
Spotify-style gapless helpers.

* `resolve_track_file(track)`  -> makes sure a queued `vid_<id>` entry has a real file
* `prefetch_next(chat_id)`      -> while song N plays, song N+1 is downloaded in background
"""
import asyncio
import logging
import os

from config import autoclean
from SHIVMUSIC import YouTube
from SHIVMUSIC.misc import db

LOGGER = logging.getLogger("PREFETCH")
_tasks: "dict[int, asyncio.Task]" = {}


def _needs_download(track: dict) -> bool:
    f = str(track.get("file") or "")
    return f.startswith("vid_")


async def resolve_track_file(track: dict, video: bool = None):
    """Return a playable path/URL for a queued track (downloads if needed)."""
    f = str(track.get("file") or "")
    if f.startswith("live_"):
        n, link = await YouTube.video(track["vidid"], True)
        return link if n else None
    if f.startswith("vid_"):
        want_video = (str(track.get("streamtype")) == "video") if video is None else bool(video)
        path, _ = await YouTube.download(track["vidid"], None, videoid=True, video=want_video or None)
        if path:
            track["file"] = path
            if path not in autoclean:
                autoclean.append(path)
        return path
    if f.startswith("index_"):
        return track.get("vidid")
    return f


async def _prefetch(chat_id: int):
    try:
        queue = db.get(chat_id) or []
        if len(queue) < 2:
            return
        nxt = queue[1]
        if _needs_download(nxt):
            await resolve_track_file(nxt)
    except Exception as e:
        LOGGER.debug("prefetch skipped: %s", e)


def prefetch_next(chat_id: int):
    """Fire-and-forget; safe to call as often as you like."""
    old = _tasks.get(chat_id)
    if old and not old.done():
        return
    try:
        _tasks[chat_id] = asyncio.create_task(_prefetch(chat_id))
    except RuntimeError:
        pass
