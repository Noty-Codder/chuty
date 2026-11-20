"""
ULTRA AUTOPLAY ENGINE  (Spotify-radio style)

How it picks the next song
--------------------------
1. Collects candidates from several sources IN PARALLEL
     - YouTube Mix of the song that just ended           (strongest signal)
     - YouTube Mix of the 1-2 songs played before it     (your "taste" in this chat)
     - same-artist search                                (style = artist)
     - fresh-artist search                               (style = discover)
2. Hard filters  : duration 60 s - 12 min, no live / jukebox / mashup / 10-hour / karaoke /
                   reaction etc., never anything already played in this chat (last 200).
3. Smart scoring : Mix rank, "seen in several sources" bonus, artist-diversity rules,
                   duration similarity to the seed, style bonus.
4. Weighted-random pick from the top results -> never the same boring loop.
5. PRE-FETCH     : while a song is still playing, the *next* autoplay track is already
                   resolved + downloaded, so the switch is instant (no gap, no "downloading").

Public API (kept compatible with the old module)
    remember_played(chat_id, vidid, title=None, artist=None)
    clear_history(chat_id)
    fetch_autoplay_track(chat_id, seed_title, seed_vidid=None, style=None)
    prepare_next(chat_id, seed_title, seed_vidid)
    pop_prepared(chat_id)
"""
import asyncio
import logging
import random
import re
import time
from typing import Dict, List, Optional

import yt_dlp

from SHIVMUSIC.platforms.Youtube import (
    _async_run,
    _base_opts,
    search_videos,
    seconds_to_str,
    time_to_seconds,
)

LOGGER = logging.getLogger("AUTOPLAY")

_HISTORY_LIMIT = 200
_MIN_SEC, _MAX_SEC = 60, 12 * 60

# chat_id -> list[{"id","key","artist","ts"}]
_history: Dict[int, List[dict]] = {}
# chat_id -> prepared next track (already resolved)
_prepared: Dict[int, dict] = {}
_prepare_tasks: Dict[int, asyncio.Task] = {}

_JUNK_TITLE = re.compile(
    r"jukebox|mashup|mash-up|non[\s-]?stop|\b\d+\s*(hour|hr|hrs)\b|full\s+album|playlist|compilation|"
    r"karaoke|reaction|tutorial|how to|podcast|interview|trailer|teaser|status|shorts|#shorts|"
    r"\blive\s+(stream|radio)|8d audio|16d|slowed|reverb|sped up|nightcore|cover by|instrumental only",
    re.I,
)
_CLEAN = re.compile(
    r"\(.*?\)|\[.*?\]|official|video|lyrics?|lyrical|audio|full song|hd|4k|\bft\.?\b.*$|\bfeat\.?\b.*$|\|.*$|-\s*topic",
    re.I,
)


# ---------------------------------------------------------------- helpers
def _key(title: str) -> str:
    t = _CLEAN.sub(" ", title or "")
    t = re.sub(r"[^\w\s]", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()[:28]


def _artist_of(entry: dict) -> str:
    a = entry.get("channel") or entry.get("uploader") or ""
    a = re.sub(r"\s*-\s*topic$|vevo$|official$", "", a, flags=re.I).strip().lower()
    return a


def remember_played(chat_id: int, vidid: str, title: str = None, artist: str = None):
    if not vidid:
        return
    hist = _history.setdefault(chat_id, [])
    hist[:] = [h for h in hist if h["id"] != vidid]
    hist.append({"id": vidid, "key": _key(title or ""), "artist": (artist or "").lower(), "ts": time.time()})
    if len(hist) > _HISTORY_LIMIT:
        del hist[: len(hist) - _HISTORY_LIMIT]


_seed_dur: Dict[str, int] = {}


def _seed_duration(chat_id: int, vidid: Optional[str]) -> int:
    return _seed_dur.get(vidid or "", 0)


def note_duration(vidid: str, seconds) -> None:
    """call.on_track_started tells us how long the seed song is (similar-length bonus in scoring)."""
    try:
        if vidid and seconds:
            if len(_seed_dur) > 500:
                _seed_dur.clear()
            _seed_dur[vidid] = int(seconds)
    except Exception:
        pass


def clear_history(chat_id: int):
    _history.pop(chat_id, None)
    _prepared.pop(chat_id, None)


def _played_ids(chat_id: int) -> set:
    return {h["id"] for h in _history.get(chat_id, [])}


def _played_keys(chat_id: int) -> set:
    return {h["key"] for h in _history.get(chat_id, []) if h["key"]}


def _recent_artists(chat_id: int, n: int = 6) -> List[str]:
    return [h["artist"] for h in _history.get(chat_id, [])[-n:] if h["artist"]]


# ---------------------------------------------------------------- sources
def _fetch_mix_sync(video_id: str, limit: int = 30) -> list:
    opts = _base_opts(["android_vr", "web"])
    opts.update({"extract_flat": True, "skip_download": True, "playlistend": limit, "noplaylist": False})
    url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return (info or {}).get("entries") or []


async def _mix(video_id: str, weight: float, tag: str) -> list:
    try:
        entries = await asyncio.wait_for(_async_run(_fetch_mix_sync, video_id), timeout=40)
    except Exception as e:
        LOGGER.debug("mix fetch failed (%s): %s", video_id, str(e)[:100])
        return []
    out = []
    for rank, e in enumerate(entries):
        if not e or not e.get("id") or not e.get("title"):
            continue
        out.append({"entry": e, "rank": rank, "weight": weight, "tag": tag})
    return out


async def _search_src(query: str, weight: float, tag: str) -> list:
    out = []
    for rank, r in enumerate(await search_videos(query, 15)):
        e = {
            "id": r["id"],
            "title": r["title"],
            "duration": r["duration_sec"],
            "channel": r["channel"],
        }
        out.append({"entry": e, "rank": rank, "weight": weight, "tag": tag})
    return out


# ---------------------------------------------------------------- core
def _score(cands: list, chat_id: int, seed_title: str, seed_artist: str, seed_dur: int, style: str) -> list:
    played_ids = _played_ids(chat_id)
    played_keys = _played_keys(chat_id)
    seed_key = _key(seed_title)
    recent = _recent_artists(chat_id)

    merged: Dict[str, dict] = {}
    for c in cands:
        e = c["entry"]
        vid = e["id"]
        title = e["title"]
        if vid in played_ids or _JUNK_TITLE.search(title):
            continue
        k = _key(title)
        if k and (k in played_keys or k == seed_key):
            continue
        try:
            dur = int(float(e.get("duration") or 0))
        except Exception:
            dur = 0
        if not dur or dur < _MIN_SEC or dur > _MAX_SEC:
            continue
        artist = _artist_of(e)
        s = c["weight"] * max(0.05, 1.0 - c["rank"] * 0.035)
        item = merged.get(vid)
        if item:
            item["score"] += s + 0.35  # appears in more than one source -> strong signal
            continue
        # duration closeness to the seed (songs of similar length = similar format)
        if seed_dur:
            s += max(0.0, 0.25 - abs(dur - seed_dur) / 900.0)
        same_artist = bool(artist and seed_artist and artist == seed_artist)
        if style == "artist":
            s += 0.8 if same_artist else -0.15
        elif style == "discover":
            s += -0.6 if (artist and artist in recent) else 0.45
        else:  # similar
            if artist and recent.count(artist) >= 2:
                s -= 0.5  # don't let one artist hog the radio
        merged[vid] = {
            "score": s,
            "vidid": vid,
            "title": title,
            "artist": artist,
            "duration_sec": dur,
            "duration_min": seconds_to_str(dur),
            "tag": c["tag"],
        }
    ranked = sorted(merged.values(), key=lambda x: x["score"], reverse=True)
    return ranked


async def _collect(chat_id: int, seed_title: str, seed_vidid: Optional[str], style: str):
    jobs = []
    hist = _history.get(chat_id, [])
    if seed_vidid:
        jobs.append(_mix(seed_vidid, 1.0, "mix"))
        if style != "artist":
            # blend with the two songs played before the seed (taste profile)
            before = [h["id"] for h in hist if h["id"] != seed_vidid][-2:]
            for hid in before:
                jobs.append(_mix(hid, 0.55, "taste"))
    clean = _CLEAN.sub(" ", seed_title or "").strip()
    seed_artist = ""
    for h in hist:
        if h["id"] == seed_vidid:
            seed_artist = h["artist"]
    if style == "artist" and (seed_artist or clean):
        jobs.append(_search_src(f"{seed_artist or clean} songs", 0.9, "artist"))
    if style == "discover" and clean:
        jobs.append(_search_src(f"{clean} similar songs new", 0.8, "discover"))
    # ALWAYS keep a plain search running in parallel: yt-dlp "Mix" often fails/blocks,
    # and without this fallback autoplay found nothing and the bot left the VC.
    if clean:
        jobs.append(_search_src(f"{clean} similar songs", 0.7 if not jobs else 0.45, "search"))
        if len(jobs) == 1:
            jobs.append(_search_src(f"{clean} songs", 0.4, "search2"))
    results = await asyncio.gather(*jobs, return_exceptions=True)
    cands = []
    for r in results:
        if isinstance(r, list):
            cands.extend(r)
    return cands, seed_artist


async def fetch_autoplay_track(chat_id: int, seed_title: str, seed_vidid: str = None, style: str = None) -> Optional[dict]:
    # 1) instant path: a track was prepared in the background while the last song played
    ready = _prepared.pop(chat_id, None)
    if ready and ready["vidid"] not in _played_ids(chat_id):
        return ready

    if style is None:
        try:
            from SHIVMUSIC.utils.database import get_autoplay_style

            style = await get_autoplay_style(chat_id)
        except Exception:
            style = "similar"

    for attempt in range(2):
        cands, seed_artist = await _collect(chat_id, seed_title, seed_vidid, style)
        seed_dur = _seed_duration(chat_id, seed_vidid)
        ranked = _score(cands, chat_id, seed_title, seed_artist, seed_dur, style)
        if ranked:
            pool = ranked[:6]
            weights = [max(0.05, p["score"]) for p in pool]
            pick = random.choices(pool, weights=weights, k=1)[0]
            vid = pick["vidid"]
            return {
                "vidid": vid,
                "title": pick["title"],
                "link": f"https://www.youtube.com/watch?v={vid}",
                "duration_min": pick["duration_min"],
                "duration_sec": pick["duration_sec"],
                "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                "artist": pick["artist"],
                "vibe": style.capitalize(),
            }
        if attempt == 0 and _history.get(chat_id):
            # everything was already played -> keep only the last few so radio never stalls
            _history[chat_id] = _history[chat_id][-5:]
    return None


# ---------------------------------------------------------------- prefetch
async def _prepare(chat_id: int, seed_title: str, seed_vidid: str):
    try:
        from SHIVMUSIC import YouTube

        track = await fetch_autoplay_track(chat_id, seed_title, seed_vidid)
        if not track:
            return
        # warm the cache so the switch is instant
        path, _ = await YouTube.download(track["vidid"], None, videoid=True)
        if path:
            track["file"] = path
            _prepared[chat_id] = track
    except Exception as e:
        LOGGER.debug("prepare_next failed: %s", e)


def prepare_next(chat_id: int, seed_title: str, seed_vidid: str):
    """Background: resolve + download the autoplay track that will follow `seed_vidid`."""
    if not seed_vidid:
        return
    cur = _prepare_tasks.get(chat_id)
    if cur and not cur.done():
        return
    if chat_id in _prepared:
        return
    try:
        _prepare_tasks[chat_id] = asyncio.create_task(_prepare(chat_id, seed_title, seed_vidid))
    except RuntimeError:
        pass


def pop_prepared(chat_id: int) -> Optional[dict]:
    return _prepared.pop(chat_id, None)


def discard_prepared(chat_id: int):
    _prepared.pop(chat_id, None)
    t = _prepare_tasks.pop(chat_id, None)
    if t and not t.done():
        t.cancel()
