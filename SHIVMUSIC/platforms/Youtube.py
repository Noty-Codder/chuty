"""
YouTube platform layer (rewritten).

What changed vs. the old version
--------------------------------
* API-ONLY downloads (Shruti + MusicSp). No cookies, no proxy needed.
  Shruti = priority 1, MusicSp = automatic fallback (ultra fast + smooth).
* yt-dlp is used only for search fallback / live-stream URLs / autoplay mix (no cookies),
  and as a download fallback ONLY if you set YT_DLP_FALLBACK=1.
* No duration / timeout limits any more (old code cut downloads at 90 s).
* Files are stored by VIDEO ID (safe for ffmpeg / shell, no unicode-title problems).
* In-flight de-duplication: prefetch + play of the same song = one download.
* `video()` now returns a direct stream URL (needed for live streams / seek).
* Every search has a yt-dlp fallback, so a broken youtubesearchpython never kills playback.
"""
import asyncio
import glob
import logging
import os
import random
import re
from typing import Optional, Union

import aiohttp
import yt_dlp
from pyrogram.enums import MessageEntityType
from pyrogram.types import Message

import config

try:  # primary (fast) search backend
    from youtubesearchpython.__future__ import Playlist, VideosSearch
except Exception:  # pragma: no cover - library missing/broken -> yt-dlp only
    Playlist = None
    VideosSearch = None

# ----------------- CONFIGURATION -----------------
DOWNLOAD_DIR = "downloads"
LOGGER = logging.getLogger("YOUTUBE")

SHRUTI_API_URL = getattr(config, "SHRUTI_API_URL", "") or os.environ.get("SHRUTI_API_URL", "")
SHRUTI_API_KEY = getattr(config, "SHRUTI_API_KEY", "") or os.environ.get("SHRUTI_API_KEY", "")
MUSICSP_API_URL = getattr(config, "MUSICSP_API_URL", "") or os.environ.get("MusicSp_API_URL", "")
MUSICSP_API_KEY = getattr(config, "MUSICSP_API_KEY", "") or os.environ.get("MusicSp_API_KEY", "")

SHRUTI_TIMEOUT = int(os.environ.get("SHRUTI_TIMEOUT", "40"))
YT_DLP_FALLBACK = os.environ.get("YT_DLP_FALLBACK", "0") == "1"

# order matters: first the clients that need no PO-token, last the ones that usually do
PLAYER_CLIENT_CHAIN = [
    ["android_vr"],
    ["tv"],
    ["web_safari"],
    ["mweb"],
    ["ios"],
    ["web"],
]

_MIN_AUDIO_BYTES = 30_000
_inflight: "dict[str, asyncio.Future]" = {}
_dl_semaphore = asyncio.Semaphore(int(os.environ.get("MAX_PARALLEL_DOWNLOADS", "4")))


# ----------------- SMALL HELPERS -----------------
def time_to_seconds(time_str) -> int:
    try:
        return sum(int(x) * 60 ** i for i, x in enumerate(reversed(str(time_str).split(":"))))
    except Exception:
        return 0


def seconds_to_str(sec) -> str:
    try:
        sec = int(float(sec))
    except Exception:
        return "0:00"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([0-9A-Za-z_-]{11})")


def extract_video_id(link: str) -> str:
    link = str(link or "").strip()
    m = _ID_RE.search(link)
    if m:
        return m.group(1)
    if "youtu.be/" in link:
        return link.split("youtu.be/")[1].split("?")[0].split("&")[0]
    if "v=" in link:
        return link.split("v=")[1].split("&")[0]
    return link


def _is_url(text: str) -> bool:
    return bool(re.match(r"https?://", str(text or "")))


async def _async_run(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


def _base_opts(clients=None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 25,
        "concurrent_fragment_downloads": 4,
        # newer yt-dlp needs a JS runtime for some YouTube formats; unknown keys are ignored
        "js_runtimes": {"deno": {}, "node": {}},
    }
    if clients:
        opts["extractor_args"] = {"youtube": {"player_client": list(clients)}}
    return opts


def _find_cached(video_id: str, video: bool = False) -> Optional[str]:
    """Return an already downloaded file for this id (any extension)."""
    if not os.path.isdir(DOWNLOAD_DIR):
        return None
    prefix = f"{video_id}"
    audio_ext = (".m4a", ".mp3", ".webm", ".opus", ".ogg", ".mp4")
    for ext in (".mp4",) if video else audio_ext:
        path = os.path.join(DOWNLOAD_DIR, f"{prefix}{ext}" if not video else f"{prefix}_video{ext}")
        if os.path.exists(path) and os.path.getsize(path) > _MIN_AUDIO_BYTES:
            return path
    return None


# ----------------- DOWNLOADERS -----------------
_session: Optional[aiohttp.ClientSession] = None


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=40, ttl_dns_cache=300, keepalive_timeout=60),
            timeout=aiohttp.ClientTimeout(total=1800, connect=6, sock_read=30),
        )
    return _session


async def external_api_download(api_url, api_key, video_id, download_type, api_name="API") -> Optional[str]:
    """Download through a private API. Returns file path or None."""
    if not api_url or not api_key:
        return None
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    is_video = download_type == "video"
    file_path = os.path.join(DOWNLOAD_DIR, f"{video_id}{'_video' if is_video else ''}.{'mp4' if is_video else 'mp3'}")
    tmp_path = f"{file_path}.{api_name}.part"
    try:
        params = {"url": video_id, "type": "video" if is_video else "audio", "api_key": api_key}
        async with _get_session().get(f"{api_url.rstrip('/')}/download", params=params) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if resp.status != 200 or "json" in ctype or "html" in ctype:
                LOGGER.warning("%s API rejected %s (status=%s, type=%s)", api_name, video_id, resp.status, ctype)
                return None
            with open(tmp_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 512):
                    f.write(chunk)
        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > _MIN_AUDIO_BYTES:
            os.replace(tmp_path, file_path)
            LOGGER.info("Downloaded %s via %s API", video_id, api_name)
            return file_path
        LOGGER.warning("%s API returned an empty/corrupt file for %s", api_name, video_id)
    except asyncio.CancelledError:
        raise  # lost the race -> cleaned up in finally
    except Exception as e:
        LOGGER.warning("%s API error for %s: %s", api_name, video_id, str(e)[:120])
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    return None


HEDGE_DELAY = float(os.environ.get("API_HEDGE_DELAY", "6"))


async def _race_apis(video_id: str, kind: str) -> Optional[str]:
    """Shruti = priority 1. If it has not answered within HEDGE_DELAY seconds (or fails) MusicSp is
    started IN PARALLEL and the first good file wins -> no more 40 s waits when one API is slow."""
    jobs = []
    if SHRUTI_API_URL and SHRUTI_API_KEY:
        jobs.append(("Shruti", SHRUTI_API_URL, SHRUTI_API_KEY))
    if MUSICSP_API_URL and MUSICSP_API_KEY:
        jobs.append(("MusicSp", MUSICSP_API_URL, MUSICSP_API_KEY))
    if not jobs:
        LOGGER.error("No API configured: set SHRUTI_API_KEY (and optionally MusicSp_API_KEY)")
        return None

    def _start(job):
        name, url, key = job
        return asyncio.create_task(external_api_download(url, key, video_id, kind, name))

    pending = {_start(jobs[0])}
    queue = list(jobs[1:])
    deadline = asyncio.get_running_loop().time() + SHRUTI_TIMEOUT
    try:
        while pending:
            timeout = HEDGE_DELAY if queue else max(1.0, deadline - asyncio.get_running_loop().time())
            done, pending = await asyncio.wait(pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                try:
                    path = t.result()
                except Exception:
                    path = None
                if path:
                    return path
            if queue and (not done or not pending):
                pending.add(_start(queue.pop(0)))  # slow / failed primary -> bring in the fallback
            elif not done and not queue:
                break  # everything left is over the time limit
        return None
    finally:
        for t in pending:
            t.cancel()


def _ytdlp_download_sync(video_id: str, video: bool) -> Optional[str]:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={video_id}"
    last_error = None
    for clients in PLAYER_CLIENT_CHAIN:
        opts = _base_opts(clients)
        if video:
            out_base = os.path.join(DOWNLOAD_DIR, f"{video_id}_video")
            opts.update({
                "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]/best",
                "merge_output_format": "mp4",
                "outtmpl": out_base + ".%(ext)s",
            })
        else:
            out_base = os.path.join(DOWNLOAD_DIR, video_id)
            opts.update({
                "format": "bestaudio[ext=m4a]/bestaudio[acodec=opus]/bestaudio/best",
                "outtmpl": out_base + ".%(ext)s",
            })
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info and info.get("is_live"):
                    return None
            found = _find_cached(video_id, video)
            if found:
                return found
        except Exception as e:  # try next client
            last_error = e
            LOGGER.warning("yt-dlp client %s failed for %s: %s", clients, video_id, str(e)[:160])
            # remove partial files left by failed attempt
            for part in glob.glob(os.path.join(DOWNLOAD_DIR, f"{video_id}*.part")):
                try:
                    os.remove(part)
                except OSError:
                    pass
    LOGGER.error("yt-dlp could not download %s: %s", video_id, str(last_error)[:200])
    return None


async def _download_unique(video_id: str, video: bool) -> Optional[str]:
    cached = _find_cached(video_id, video)
    if cached:
        return cached

    key = f"{video_id}:{'v' if video else 'a'}"
    running = _inflight.get(key)
    if running is not None:
        try:
            return await asyncio.shield(running)
        except Exception:
            return None

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _inflight[key] = fut
    path = None
    try:
        async with _dl_semaphore:
            kind = "video" if video else "audio"
            for _attempt in range(2):  # one quick retry if both APIs hiccup
                path = await _race_apis(video_id, kind)
                if path:
                    break
                await asyncio.sleep(0.4)
            if not path and YT_DLP_FALLBACK:
                path = await _async_run(_ytdlp_download_sync, video_id, video)
        return path
    except asyncio.CancelledError:
        raise
    except Exception as e:
        LOGGER.error("download failed for %s: %s", video_id, e)
        return None
    finally:
        # ALWAYS resolve the future (even when cancelled) so nobody waits forever
        if not fut.done():
            fut.set_result(path)
        _inflight.pop(key, None)


async def download_song(link: str, title: str = None) -> Optional[str]:
    video_id = extract_video_id(link)
    if not video_id or len(video_id) < 3:
        return None
    return await _download_unique(video_id, False)


async def download_video(link: str, title: str = None) -> Optional[str]:
    video_id = extract_video_id(link)
    if not video_id or len(video_id) < 3:
        return None
    return await _download_unique(video_id, True)


# ----------------- SEARCH (primary + yt-dlp fallback) -----------------
def _entry_to_result(entry: dict) -> Optional[dict]:
    if not entry:
        return None
    vidid = entry.get("id")
    title = entry.get("title")
    if not vidid or not title:
        return None
    dur = entry.get("duration")
    try:
        dur_sec = int(float(dur)) if dur else 0
    except Exception:
        dur_sec = 0
    return {
        "id": vidid,
        "title": title,
        "duration": seconds_to_str(dur_sec) if dur_sec else None,
        "duration_sec": dur_sec,
        "thumb": f"https://img.youtube.com/vi/{vidid}/hqdefault.jpg",
        "channel": entry.get("channel") or entry.get("uploader") or "Unknown",
        "link": f"https://www.youtube.com/watch?v={vidid}",
        "live": bool(entry.get("is_live")) or entry.get("live_status") == "is_live",
    }


def _ytdlp_search_sync(query: str, limit: int) -> list:
    opts = _base_opts(["android_vr", "web"])
    opts.update({"extract_flat": "in_playlist", "skip_download": True})
    if _is_url(query):
        target = query
    else:
        target = f"ytsearch{limit}:{query}"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)
    entries = []
    if info:
        entries = info.get("entries") or [info]
    out = []
    for e in entries:
        item = _entry_to_result(e)
        if item:
            out.append(item)
    return out[:limit]


_SEARCH_CACHE: "dict[tuple, tuple]" = {}
_SEARCH_TTL = 600


async def search_videos(query: str, limit: int = 5) -> list:
    """Unified search result list (normalised dicts). Never raises. Cached for 10 min."""
    query = str(query or "").strip()
    if not query:
        return []
    ckey = (query.lower(), int(limit))
    hit = _SEARCH_CACHE.get(ckey)
    now = asyncio.get_running_loop().time()
    if hit and now - hit[0] < _SEARCH_TTL:
        return [dict(x) for x in hit[1]]
    res = await _search_videos_uncached(query, limit)
    if res:
        if len(_SEARCH_CACHE) > 300:
            for k in sorted(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k][0])[:100]:
                _SEARCH_CACHE.pop(k, None)
        _SEARCH_CACHE[ckey] = (now, [dict(x) for x in res])
    return res


async def _search_videos_uncached(query: str, limit: int = 5) -> list:
    if VideosSearch is not None:
        try:
            res = await asyncio.wait_for(VideosSearch(query, limit=limit).next(), timeout=15)
            items = []
            for r in (res or {}).get("result", []) or []:
                dur = r.get("duration")
                thumbs = r.get("thumbnails") or []
                items.append({
                    "id": r.get("id"),
                    "title": r.get("title") or "Unknown Title",
                    "duration": dur,
                    "duration_sec": time_to_seconds(dur) if dur else 0,
                    "thumb": thumbs[0]["url"].split("?")[0] if thumbs else f"https://img.youtube.com/vi/{r.get('id')}/hqdefault.jpg",
                    "channel": (r.get("channel") or {}).get("name") or "Unknown",
                    "link": r.get("link") or f"https://www.youtube.com/watch?v={r.get('id')}",
                    "live": dur is None,
                })
            items = [i for i in items if i["id"]]
            if items:
                return items
        except Exception as e:
            LOGGER.warning("youtubesearchpython failed (%s) -> yt-dlp fallback", str(e)[:100])
    try:
        return await _async_run(_ytdlp_search_sync, query, limit)
    except Exception as e:
        LOGGER.error("yt-dlp search failed: %s", str(e)[:160])
        return []


def _stream_url_sync(video_id: str) -> Optional[str]:
    url = f"https://www.youtube.com/watch?v={video_id}"
    for clients in PLAYER_CLIENT_CHAIN:
        opts = _base_opts(clients)
        opts["format"] = "best[height<=?720][width<=?1280]/best"
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if info and info.get("url"):
                return info["url"]
            for f in reversed((info or {}).get("formats") or []):
                if f.get("url") and f.get("vcodec") != "none":
                    return f["url"]
        except Exception as e:
            LOGGER.warning("stream-url client %s failed: %s", clients, str(e)[:120])
    return None


# ----------------- YOUTUBE API CLASS -----------------
class YouTubeAPI:
    def __init__(self):
        self.base = "https://www.youtube.com/watch?v="
        self.regex = r"(?:youtube\.com|youtu\.be)"
        self.status = "https://www.youtube.com/oembed?url="
        self.listbase = "https://youtube.com/playlist?list="
        self.reg = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

    async def exists(self, link: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + link
        return bool(re.search(self.regex, link))

    async def url(self, message_1: Message) -> Union[str, None]:
        messages = [message_1]
        if message_1.reply_to_message:
            messages.append(message_1.reply_to_message)
        for message in messages:
            text = message.text or message.caption or ""
            entities = (message.entities or []) + (message.caption_entities or [])
            for entity in entities:
                if entity.type == MessageEntityType.URL:
                    return text[entity.offset: entity.offset + entity.length]
                if entity.type == MessageEntityType.TEXT_LINK:
                    return entity.url
        return None

    def _prep(self, link: str, videoid) -> str:
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        return link

    async def _first(self, link: str):
        results = await search_videos(link, 1)
        return results[0] if results else None

    async def details(self, link: str, videoid: Union[bool, str] = None):
        link = self._prep(link, videoid)
        r = await self._first(link)
        if not r:
            return None, None, None, None, None
        return r["title"], r["duration"], r["duration_sec"], r["thumb"], r["id"]

    async def title(self, link: str, videoid: Union[bool, str] = None):
        r = await self._first(self._prep(link, videoid))
        return r["title"] if r else "Unknown Title"

    async def duration(self, link: str, videoid: Union[bool, str] = None):
        r = await self._first(self._prep(link, videoid))
        return (r["duration"] or "0:00") if r else "0:00"

    async def thumbnail(self, link: str, videoid: Union[bool, str] = None):
        vid = link if videoid else extract_video_id(link)
        r = await self._first(self._prep(link, videoid))
        if r:
            return r["thumb"]
        if vid and len(vid) > 5:
            return f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"
        return "https://telegra.ph/file/2e3d368e77c449c287430.jpg"

    async def video(self, link: str, videoid: Union[bool, str] = None):
        """Returns (1, direct_stream_url) -- works for live + normal videos."""
        vid = link if videoid else extract_video_id(link)
        vid = extract_video_id(vid)
        try:
            url = await _async_run(_stream_url_sync, vid)
            if url:
                return 1, url
            downloaded = await download_video(vid)
            if downloaded:
                return 1, downloaded
            return 0, "Video stream could not be resolved"
        except Exception as e:
            return 0, f"Video error: {e}"

    async def playlist(self, link, limit, user_id, videoid: Union[bool, str] = None):
        if videoid:
            link = self.listbase + link
        if "&" in link:
            link = link.split("&")[0]
        ids = []
        if Playlist is not None:
            try:
                plist = await asyncio.wait_for(Playlist.get(link), timeout=25)
                for data in (plist.get("videos") or [])[:limit]:
                    if data and data.get("id"):
                        ids.append(data["id"])
            except Exception as e:
                LOGGER.warning("Playlist.get failed (%s) -> yt-dlp fallback", str(e)[:100])
        if ids:
            return ids

        def _flat():
            opts = _base_opts(["android_vr", "web"])
            opts.update({"extract_flat": "in_playlist", "noplaylist": False, "playlistend": limit})
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(link, download=False)

        try:
            info = await _async_run(_flat)
            for e in (info or {}).get("entries") or []:
                if e and e.get("id"):
                    ids.append(e["id"])
        except Exception as e:
            LOGGER.error("yt-dlp playlist failed: %s", str(e)[:160])
        return ids[:limit]

    async def track(self, link: str, videoid: Union[bool, str] = None):
        link = self._prep(link, videoid)
        r = await self._first(link)
        if not r:
            return None, None
        return {
            "title": r["title"],
            "link": r["link"],
            "vidid": r["id"],
            "duration_min": r["duration"],
            "thumb": r["thumb"],
        }, r["id"]

    async def formats(self, link: str, videoid: Union[bool, str] = None):
        link = self._prep(link, videoid)
        formats_available = []

        def _info():
            with yt_dlp.YoutubeDL(_base_opts(["android_vr", "web"])) as ydl:
                return ydl.extract_info(link, download=False)

        try:
            r = await _async_run(_info)
            for f in (r or {}).get("formats", []):
                if "dash" in str(f.get("format", "")).lower():
                    continue
                formats_available.append({
                    "format": f.get("format"),
                    "filesize": f.get("filesize"),
                    "format_id": f.get("format_id"),
                    "ext": f.get("ext"),
                    "format_note": f.get("format_note"),
                    "yturl": link,
                })
        except Exception:
            pass
        return formats_available, link

    async def slider(self, link: str, query_type: int, videoid: Union[bool, str] = None):
        raw = link if videoid else extract_video_id(link)
        link = self._prep(link, videoid)
        results = await search_videos(link, 10)
        try:
            r = results[query_type]
            return r["title"], r["duration"] or "0:00", r["thumb"], r["id"]
        except Exception:
            fallback = f"https://img.youtube.com/vi/{raw}/hqdefault.jpg" if raw and len(raw) > 5 else "https://telegra.ph/file/2e3d368e77c449c287430.jpg"
            return "Unknown Title", "0:00", fallback, "None"

    async def download(
        self, link: str, mystic, video: Union[bool, str] = None, videoid: Union[bool, str] = None,
        songaudio: Union[bool, str] = None, songvideo: Union[bool, str] = None,
        format_id: Union[bool, str] = None, title: Union[bool, str] = None,
    ):
        if videoid:
            link = self.base + link
        try:
            path = await (download_video(link) if video else download_song(link))
            if path:
                return path, True
            return None, False
        except Exception as e:
            LOGGER.error("YouTubeAPI.download error: %s", e)
            return None, False

    async def autoplay(self, last_vidid: str, title: str, max_duration: int = None):
        """Kept for backward compatibility -> delegates to the new autoplay engine."""
        try:
            from SHIVMUSIC.utils.autoplay import fetch_autoplay_track
            t = await fetch_autoplay_track(0, title, last_vidid)
            if not t:
                return None
            return {
                "vidid": t["vidid"],
                "title": t["title"],
                "duration_min": t["duration_min"],
                "duration_sec": time_to_seconds(t["duration_min"]),
            }
        except Exception as e:
            LOGGER.error("autoplay compat error: %s", e)
            return None


YouTube = YouTubeAPI()
