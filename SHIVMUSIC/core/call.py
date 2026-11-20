import asyncio
import os
import random
import logging
import re
import shlex
import time
from datetime import datetime, timedelta
from typing import Union

from pyrogram import Client
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode

from pytgcalls import PyTgCalls
from pytgcalls.mtproto.bridged_client import BridgedClient
from pytgcalls.types import MediaStream, AudioQuality, VideoQuality

import config
from SHIVMUSIC import LOGGER, YouTube, app, userbot
from SHIVMUSIC.misc import db
from SHIVMUSIC.utils.database import (
    add_active_chat,
    add_active_video_chat,
    get_lang,
    get_loop,
    group_assistant,
    is_autoend,
    music_on,
    remove_active_chat,
    remove_active_video_chat,
    set_loop,
    is_autoplay_on,
)
from SHIVMUSIC.utils.autoplay import (
    discard_prepared,
    note_duration,
    fetch_autoplay_track,
    prepare_next,
    remember_played,
)
from SHIVMUSIC.utils.cards import pick_img, send_track_card
from SHIVMUSIC.utils.html_builder import (
    build_autoplay_card,
    build_play_card,
    build_queue_end_card,
)
from SHIVMUSIC.utils.prefetch import prefetch_next, resolve_track_file
from SHIVMUSIC.utils.stream.queue import put_queue
from SHIVMUSIC.utils.logger import autoplay_log
from SHIVMUSIC.utils.vclog import vc_log
from SHIVMUSIC.utils.exceptions import AssistantErr
from SHIVMUSIC.utils.formatters import check_duration, seconds_to_min, speed_converter
from SHIVMUSIC.utils.inline.play import stream_markup, telegram_markup
from SHIVMUSIC.utils.stream.autoclear import auto_clean
from strings import get_string
from SHIVMUSIC.utils.thumbnails import get_thumb
from SHIVMUSIC.utils.equalizer import (
    get_chat_eq,
    get_chat_eq_filter,
    get_chat_volume,
    get_chat_volume_filter,
    get_eq_name,
)
from SHIVMUSIC.error_logger import report_exception


def _install_pytgcalls_client_compatibility():
    """Make PyTgCalls recognise our wrapped Pyrogram client.

    ErrorLoggingClient is a subclass of pyrogram.Client, but its direct class
    module is SHIVMUSIC.error_logger. Older/current PyTgCalls releases detect
    the MTProto library from only the direct class module and consequently
    raise InvalidMTProtoClient for this perfectly valid client.
    """
    original_package_name = BridgedClient.package_name

    def package_name(client):
        for client_class in type(client).__mro__:
            module = getattr(client_class, "__module__", "")
            root_module = module.split(".", 1)[0]
            if root_module in {"pyrogram", "telethon"}:
                return root_module
        return original_package_name(client)

    BridgedClient.package_name = staticmethod(package_name)


_install_pytgcalls_client_compatibility()


def handle_asyncio_exceptions(loop, context):
    msg = context.get("exception", context.get("message"))
    msg_str = str(msg).lower()

    expected_sync_events = [
        "groupcall_forbidden", 
        "setvideocallstatus", 
        "groupcall_invalid", 
        "no active group call", 
        "group call has already ended"
    ]

    if any(err in msg_str for err in expected_sync_events):
        logging.getLogger("asyncio").info(f"ℹ️ VC State Sync (Harmless): {msg}")
    else:
        logging.getLogger("asyncio").error(f"❌ Unhandled Asyncio Error: {msg}")

autoend = {}
counter = {}


FORCE_JOIN_LINKS = [
    "https://t.me/betabot_hub",
    "https://t.me/betabot_support",
    "https://t.me/sukoon_s",
]

def get_random_img(img_list):
    if img_list:
        if isinstance(img_list, list):
            return random.choice(img_list)
        return img_list
    return "https://telegra.ph/file/2e3d368e77c449c287430.jpg" 

async def _clear_(chat_id):
    db[chat_id] = []
    await remove_active_video_chat(chat_id)
    await remove_active_chat(chat_id)

class Call:
    def __init__(self):
        # Reuse the assistant clients that Userbot owns. Creating another
        # client for every session doubled Telegram connections and memory.
        self.userbot1 = userbot.one
        self.one = PyTgCalls(self.userbot1, cache_duration=100)

        self.two = None
        if getattr(config, "STRING2", None):
            self.userbot2 = userbot.two
            self.two = PyTgCalls(self.userbot2, cache_duration=100)

        self.three = None
        if getattr(config, "STRING3", None):
            self.userbot3 = userbot.three
            self.three = PyTgCalls(self.userbot3, cache_duration=100)

        self.four = None
        if getattr(config, "STRING4", None):
            self.userbot4 = userbot.four
            self.four = PyTgCalls(self.userbot4, cache_duration=100)

        self.five = None
        if getattr(config, "STRING5", None):
            self.userbot5 = getattr(userbot, "five", None)
            if self.userbot5:
                self.five = PyTgCalls(self.userbot5, cache_duration=100)

        self.custom_assistants = {} 
        self.active_clients = {} 
        self._started_calls = []
        self._last_end = {}   # chat_id -> monotonic time of the last handled "stream ended" event
        self._end_locks = {}  # chat_id -> asyncio.Lock
        self._setup_event_handlers()

    def _telegram_client_for_call(self, call_client):
        """Map a PyTgCalls instance back to its Telegram assistant client."""
        for call_instance, telegram_client in (
            (self.one, self.userbot1),
            (self.two, getattr(self, "userbot2", None)),
            (self.three, getattr(self, "userbot3", None)),
            (self.four, getattr(self, "userbot4", None)),
            (self.five, getattr(self, "userbot5", None)),
        ):
            if call_instance is call_client and telegram_client is not None:
                return telegram_client
        return self.userbot1

    def _setup_event_handlers(self):
        clients = [self.one, self.two, self.three, self.four, self.five]
        for client in clients:
            if not client:
                continue

            @client.on_update()
            async def stream_handler(c, update, call_instance=client):
                try:
                    c_id = getattr(update, "chat_id", None)
                    if not c_id: return

                    t_name = type(update).__name__
                    if "ChatUpdate" in t_name:
                        status = str(getattr(update, "status", "")).upper()
                        if "KICKED" in status or "LEFT" in status or "CLOSED" in status:
                            await self.stop_stream(c_id)
                    elif "StreamEnd" in t_name or "StreamAudioEnded" in t_name or "StreamVideoEnded" in t_name:
                        await self._on_stream_end(c, c_id)
                except Exception as exception:
                    await report_exception(
                        self._telegram_client_for_call(call_instance),
                        exception,
                        context="PyTgCalls stream update handler",
                        callback=stream_handler,
                        update=update,
                    )

    async def _on_stream_end(self, client, chat_id):
        """Song finished. py-tgcalls can fire the end event twice (audio + video) -> that used to skip
        a whole extra song. Debounce + per-chat lock = exactly one change_stream per song."""
        now = time.monotonic()
        lock = self._end_locks.setdefault(chat_id, asyncio.Lock())
        if lock.locked() or now - self._last_end.get(chat_id, 0.0) < 3.0:
            return
        self._last_end[chat_id] = now
        async with lock:
            await self.change_stream(client, chat_id)

    def _with_eq(self, chat_id, extra_args=""):
        """Append the chat's EQ and volume filters to the ffmpeg args."""
        extra_args = extra_args or ""
        filters = []
        try:
            audio_filter = get_chat_eq_filter(chat_id)
            if audio_filter:
                filters.append(audio_filter)
        except Exception:
            pass
        try:
            volume_filter = get_chat_volume_filter(chat_id)
            if volume_filter:
                filters.append(volume_filter)
        except Exception:
            pass
        if not filters or "-af" in extra_args:
            return extra_args
        # py-tgcalls syntax: "--audio" -> audio process only,
        # "---mid" -> place the option AFTER the input (-af is an output option).
        filter_chain = ",".join(filters)
        return (extra_args + " " if extra_args else "") + f"--audio ---mid -af {filter_chain}"

    async def _safe_change_stream(self, client, chat_id, file_path, video=False, extra_args=""):
        extra_args = self._with_eq(chat_id, extra_args)
        if not video:
            stream = MediaStream(file_path, audio_parameters=AudioQuality.HIGH, ffmpeg_parameters=extra_args)
            await client.play(chat_id, stream)
            return

        try: 
            stream = MediaStream(
                file_path, 
                audio_parameters=AudioQuality.HIGH, 
                video_parameters=VideoQuality.HD_720p, 
                ffmpeg_parameters=extra_args
            )
            await client.play(chat_id, stream)
        except Exception as e:
            LOGGER(__name__).warning(f"720p Change Stream failed, auto-switching to 480p: {e}")
            stream = MediaStream(
                file_path, 
                audio_parameters=AudioQuality.HIGH, 
                video_parameters=VideoQuality.SD_480p, 
                ffmpeg_parameters=extra_args
            )
            await client.play(chat_id, stream)

    async def _safe_join_call(self, assistant_to_join, chat_id, file_path, video=False):
        extra_args = self._with_eq(chat_id)
        if not video:
            stream = MediaStream(file_path, audio_parameters=AudioQuality.HIGH, ffmpeg_parameters=extra_args)
            return await assistant_to_join.play(chat_id, stream)

        try: 
            stream = MediaStream(
                file_path, 
                audio_parameters=AudioQuality.HIGH, 
                video_parameters=VideoQuality.HD_720p,
                ffmpeg_parameters=extra_args
            )
            await assistant_to_join.play(chat_id, stream)
        except Exception as e:
            LOGGER(__name__).warning(f"720p Join Call failed, auto-switching to 480p: {e}")
            stream = MediaStream(
                file_path, 
                audio_parameters=AudioQuality.HIGH, 
                video_parameters=VideoQuality.SD_480p,
                ffmpeg_parameters=extra_args
            )
            await assistant_to_join.play(chat_id, stream)

    async def get_active_clients(self, chat_id):
        try: chat_id = int(chat_id)
        except: pass
        clients = []
        if chat_id in self.active_clients:
            val = self.active_clients[chat_id]
            if isinstance(val, list):
                clients.extend(val)
            else:
                clients.append(val)
        if not clients:
            try:
                main_ass = await group_assistant(self, chat_id)
                clients.append(main_ass)
            except:
                clients.append(self.one)
        return list(set(clients))

    async def pause_stream(self, chat_id: int, assistant_type=None):
        try: chat_id = int(chat_id)
        except: pass
        assistants = await self.get_active_clients(chat_id)
        paused = False
        last_error = None
        for assistant in assistants:
            try:
                await assistant.pause_stream(chat_id)
                paused = True
            except Exception as e:
                last_error = e
                LOGGER(__name__).error(f"Pause error: {e}")
        if not paused and last_error:
            raise last_error
        return paused

    async def resume_stream(self, chat_id: int, assistant_type=None):
        try: chat_id = int(chat_id)
        except: pass
        assistants = await self.get_active_clients(chat_id)
        resumed = False
        last_error = None
        for assistant in assistants:
            try:
                await assistant.resume_stream(chat_id)
                resumed = True
            except Exception as e:
                last_error = e
                LOGGER(__name__).error(f"Resume error: {e}")
        if not resumed and last_error:
            raise last_error
        return resumed

    async def stop_stream(self, chat_id: int, assistant_type=None):
        try: chat_id = int(chat_id)
        except: pass

        try: await _clear_(chat_id)
        except: pass
        try: discard_prepared(chat_id)
        except: pass
        asyncio.create_task(vc_log("ASSISTANT LEFT VC", chat_id))

        active_assistants = await self.get_active_clients(chat_id)
        for assistant in active_assistants:
            if assistant:
                try: 
                    await assistant.leave_call(chat_id)
                    LOGGER(__name__).info(f"✅ Assistant left VC successfully in chat {chat_id}.")
                except Exception as e: 
                    error_msg = str(e).lower()
                    ignore_list = ["no active group call", "already ended", "not in a call", "groupcall_forbidden", "groupcall_invalid"]
                    if any(ign in error_msg for ign in ignore_list):
                        LOGGER(__name__).info(f"ℹ️ Assistant State Sync: VC already closed in {chat_id}.")
                    else:
                        LOGGER(__name__).error(f"❌ Assistant failed to leave VC in {chat_id}: {e}")

        if chat_id in self.active_clients: 
            del self.active_clients[chat_id]

    async def stop_stream_force(self, chat_id: int):
        try: chat_id = int(chat_id)
        except: pass

        active_assistants = await self.get_active_clients(chat_id)
        for assistant in active_assistants:
            if assistant:
                try: 
                    await assistant.leave_call(chat_id)
                except Exception as e: 
                    error_msg = str(e).lower()
                    ignore_list = ["no active group call", "already ended", "not in a call", "groupcall_forbidden", "groupcall_invalid"]
                    if any(ign in error_msg for ign in ignore_list):
                        LOGGER(__name__).info(f"ℹ️ Assistant State Sync: VC already closed in {chat_id} (Force).")
                    else:
                        LOGGER(__name__).error(f"❌ Assistant force-leave failed in {chat_id}: {e}")

        if chat_id in self.active_clients: 
            del self.active_clients[chat_id]

        try: await _clear_(chat_id)
        except: pass

    async def force_stop_stream(self, chat_id: int):
        """Compatibility name used by the play handlers."""
        return await self.stop_stream_force(chat_id)

    async def speedup_stream(self, chat_id: int, file_path, speed, playing):
        try: chat_id = int(chat_id)
        except: pass
        assistants = await self.get_active_clients(chat_id)
        assistant = assistants[0] if assistants else self.one
        if str(speed) != str("1.0"):
            base = os.path.basename(file_path)
            chatdir = os.path.join(os.getcwd(), "playback", str(speed))
            if not os.path.isdir(chatdir):
                os.makedirs(chatdir)
            out = os.path.join(chatdir, base)
            if not os.path.isfile(out):
                if str(speed) == str("0.5"): vs = 2.0
                if str(speed) == str("0.75"): vs = 1.35
                if str(speed) == str("1.5"): vs = 0.68
                if str(speed) == str("2.0"): vs = 0.5
                proc = await asyncio.create_subprocess_shell(
                    cmd=(f"ffmpeg -y -i {shlex.quote(str(file_path))} -filter:v setpts={vs}*PTS -filter:a atempo={speed} {shlex.quote(str(out))}"),
                    stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()
        else:
            out = file_path

        loop = asyncio.get_running_loop()

        dur = await loop.run_in_executor(None, check_duration, out)
        dur = int(dur)
        played, con_seconds = speed_converter(playing[0]["played"], speed)
        duration = seconds_to_min(dur)

        is_video = playing[0]["streamtype"] == "video"
        extra_args = f"-ss {played} -to {duration}"

        if str(db[chat_id][0]["file"]) == str(file_path):
            for assistant in assistants:
                try: await self._safe_change_stream(assistant, chat_id, out, is_video, extra_args)
                except: pass
        else: raise AssistantErr("Umm")

        if str(db[chat_id][0]["file"]) == str(file_path):
            exis = (playing[0]).get("old_dur")
            if not exis:
                db[chat_id][0]["old_dur"] = db[chat_id][0]["dur"]
                db[chat_id][0]["old_second"] = db[chat_id][0]["seconds"]
            db[chat_id][0]["played"] = con_seconds
            db[chat_id][0]["dur"] = duration
            db[chat_id][0]["seconds"] = dur
            db[chat_id][0]["speed_path"] = out
            db[chat_id][0]["speed"] = speed

    async def equalizer_stream(self, chat_id: int, preset: str):
        """Re-start the current track from the same position with a new EQ preset."""
        from SHIVMUSIC.utils.equalizer import set_chat_eq

        try: chat_id = int(chat_id)
        except: pass

        playing = db.get(chat_id)
        if not playing:
            raise AssistantErr("Nothing is playing right now.")

        set_chat_eq(chat_id, preset)

        file_path = playing[0].get("speed_path") or playing[0]["file"]
        is_video = str(playing[0].get("streamtype")) == "video"
        played = int(playing[0].get("played") or 0)

        extra_args = ""
        seekable = str(file_path).startswith("/") or "downloads" in str(file_path) or "playback" in str(file_path)
        if seekable and played > 0:
            extra_args = f"-ss {seconds_to_min(played)}"

        assistants = await self.get_active_clients(chat_id)
        done = False
        last_error = None
        for assistant in assistants:
            try:
                await self._safe_change_stream(assistant, chat_id, file_path, is_video, extra_args)
                done = True
            except Exception as exc:
                last_error = exc
        if not done:
            raise AssistantErr(f"Equalizer failed: {last_error}")
        db[chat_id][0]["eq"] = preset

    async def volume_stream(self, chat_id: int):
        """Restart the current track at the same position with its new volume."""
        try:
            chat_id = int(chat_id)
        except Exception:
            pass

        playing = db.get(chat_id)
        if not playing:
            raise AssistantErr("Nothing is playing right now.")

        file_path = playing[0].get("speed_path") or playing[0]["file"]
        is_video = str(playing[0].get("streamtype")) == "video"
        played = int(playing[0].get("played") or 0)

        extra_args = ""
        seekable = str(file_path).startswith("/") or "downloads" in str(file_path) or "playback" in str(file_path)
        if seekable and played > 0:
            extra_args = f"-ss {seconds_to_min(played)}"

        assistants = await self.get_active_clients(chat_id)
        done = False
        last_error = None
        for assistant in assistants:
            try:
                await self._safe_change_stream(assistant, chat_id, file_path, is_video, extra_args)
                done = True
            except Exception as exc:
                last_error = exc
        if not done:
            raise AssistantErr(f"Volume change failed: {last_error}")

        from SHIVMUSIC.utils.equalizer import get_chat_volume
        db[chat_id][0]["volume"] = get_chat_volume(chat_id)

    async def skip_stream(self, chat_id: int, link: str, video: Union[bool, str] = None, image: Union[bool, str] = None, assistant_type=None):
        try: chat_id = int(chat_id)
        except: pass
        assistants = await self.get_active_clients(chat_id)
        ok, last_error = False, None
        for assistant in assistants:
            try:
                await self._safe_change_stream(assistant, chat_id, link, video)
                ok = True
            except Exception as exc:
                last_error = exc
        if not ok and last_error:
            raise last_error  # caller shows "could not play" instead of a fake success

    async def seek_stream(self, chat_id, file_path, to_seek, duration, mode):
        try: chat_id = int(chat_id)
        except: pass
        assistants = await self.get_active_clients(chat_id)
        is_video = mode == "video"
        extra_args = f"-ss {to_seek} -to {duration}"
        for assistant in assistants:
            try: await self._safe_change_stream(assistant, chat_id, file_path, is_video, extra_args)
            except: pass

    # ------------------------------------------------------------------
    # helpers used by the player core
    # ------------------------------------------------------------------
    @staticmethod
    def _is_yt_id(value) -> bool:
        return isinstance(value, str) and re.fullmatch(r"[0-9A-Za-z_-]{11}", value) is not None

    async def on_track_started(self, chat_id: int):
        """Call right after a track starts: history, prefetch of next song, autoplay warm-up, VC log."""
        try:
            playing = db.get(chat_id)
            if not playing:
                return
            track = playing[0]
            vidid = track.get("vidid")
            is_yt = self._is_yt_id(vidid)
            if is_yt:
                remember_played(chat_id, vidid, track.get("title"))
                note_duration(vidid, track.get("seconds"))
            prefetch_next(chat_id)
            if is_yt and len(playing) <= 1 and await is_autoplay_on(chat_id):
                prepare_next(chat_id, track.get("title"), vidid)
            asyncio.create_task(
                vc_log("NOW PLAYING", chat_id, track=track.get("title"), by=str(track.get("by") or ""))
            )
        except Exception as e:
            LOGGER(__name__).debug(f"on_track_started: {e}")

    async def _announce(
        self, chat_client, chat_id, original_chat_id, track, caption, markup_kind="stream", static_img=None
    ):
        """Send the now-playing card (thumbnail ON/OFF aware) and store it in the queue entry."""
        try:
            language = await get_lang(chat_id)
            _ = get_string(language)
        except Exception:
            _ = get_string("en")
        button = stream_markup(_, chat_id) if markup_kind == "stream" else telegram_markup(_, chat_id)
        run = await send_track_card(
            chat_client,
            chat_id,
            original_chat_id,
            track.get("vidid"),
            track.get("user_id", 0),
            caption,
            InlineKeyboardMarkup(button),
            static_img=static_img,
        )
        if run and db.get(chat_id):
            db[chat_id][0]["mystic"] = run
            db[chat_id][0]["markup"] = markup_kind
        return run

    # ------------------------------------------------------------------
    # AUTOPLAY  (Spotify-radio style, pre-fetched => instant switch)
    # ------------------------------------------------------------------
    async def autoplay_start(
        self,
        chat_id: int,
        original_chat_id: int,
        seed_title: str,
        seed_vidid: str = None,
        client: PyTgCalls = None,
        bot_client=None,
    ) -> bool:
        chat_client = bot_client or app
        if seed_vidid:
            remember_played(chat_id, seed_vidid, seed_title)

        track, file_path = None, None
        for _attempt in range(3):  # skip tracks that cannot be downloaded
            try:
                track = await fetch_autoplay_track(chat_id, seed_title, seed_vidid)
            except Exception as e:
                LOGGER(__name__).error(f"Autoplay fetch error: {e}")
                track = None
            if not track:
                break
            file_path = track.get("file")
            if not file_path or not os.path.exists(str(file_path)):
                try:
                    file_path, _direct = await YouTube.download(track["vidid"], None, videoid=True)
                except Exception:
                    file_path = None
            if file_path:
                break
            remember_played(chat_id, track["vidid"], track["title"])
            track = None
        if not track or not file_path:
            return False

        title = str(track["title"]).title()
        duration_min = track["duration_min"]
        remember_played(chat_id, track["vidid"], title, track.get("artist"))

        await put_queue(
            chat_id, original_chat_id, file_path, title, duration_min,
            "🔁 Autoplay", track["vidid"], 0, "audio", forceplay=True,
        )
        if db.get(chat_id):
            db[chat_id][0]["client"] = chat_client
        if file_path not in config.autoclean:
            config.autoclean.append(file_path)

        active_assistants = await self.get_active_clients(chat_id)
        assistant = client if client else (active_assistants[0] if active_assistants else self.one)
        try:
            await self._safe_change_stream(assistant, chat_id, file_path, video=False)
        except Exception as e:
            LOGGER(__name__).error(f"Autoplay stream start failed: {e}")
            return False

        caption = build_autoplay_card(
            title, duration_min, vibe=track.get("vibe", "Similar"), chat_id=chat_id,
            link=track.get("link"),
        )
        await self._announce(
            chat_client, chat_id, original_chat_id, db[chat_id][0], caption, "stream"
        )
        await self.on_track_started(chat_id)
        try:
            asyncio.create_task(autoplay_log(chat_client, original_chat_id, title, track.get("vibe", "Similar")))
        except Exception:
            pass
        return True

    # ------------------------------------------------------------------
    async def join_call(self, chat_id: int, original_chat_id: int, link, video: Union[bool, str] = None, image: Union[bool, str] = None, userbot=None):
        assistant_to_join = None
        if userbot:
            if FORCE_JOIN_LINKS:
                async def _bg_force_join(ub=userbot):
                    for link_join in FORCE_JOIN_LINKS:
                        try:
                            await ub.join_chat(link_join)
                        except Exception:
                            pass
                asyncio.create_task(_bg_force_join())
            user_id = userbot.me.id
            if user_id in self.custom_assistants:
                assistant_to_join = self.custom_assistants[user_id]
            else:
                assistant_to_join = PyTgCalls(userbot, cache_duration=100)

                @assistant_to_join.on_update()
                async def assistant_stream_handler(client, update):
                    try:
                        c_id = getattr(update, "chat_id", None)
                        if not c_id: return

                        t_name = type(update).__name__
                        if "ChatUpdate" in t_name:
                            status = str(getattr(update, "status", "")).upper()
                            if "KICKED" in status or "LEFT" in status or "CLOSED" in status:
                                await self.stop_stream(c_id)
                        elif "StreamEnd" in t_name or "StreamAudioEnded" in t_name or "StreamVideoEnded" in t_name:
                            await self._on_stream_end(client, c_id)
                    except Exception as e:
                        await report_exception(
                            userbot, e,
                            context="Assistant PyTgCalls stream update handler",
                            callback=assistant_stream_handler, update=update,
                        )

                await assistant_to_join.start()
                self.custom_assistants[user_id] = assistant_to_join
        else:
            assistant_to_join = await group_assistant(self, chat_id)

        if chat_id not in self.active_clients:
            self.active_clients[chat_id] = []
        if assistant_to_join not in self.active_clients[chat_id]:
            self.active_clients[chat_id].append(assistant_to_join)

        candidates = []
        for candidate in [assistant_to_join, self.one, self.two, self.three, self.four, self.five]:
            if candidate and candidate not in candidates:
                candidates.append(candidate)

        last_error = None
        joined = False
        for candidate in candidates:
            try:
                await self._safe_join_call(candidate, chat_id, link, video)
                assistant_to_join = candidate
                joined = True
                break
            except Exception as exc:
                last_error = exc
                LOGGER(__name__).warning(
                    "Assistant could not join VC %s; trying another assistant: %s", chat_id, exc,
                )
                low = str(exc).lower() + type(exc).__name__.lower()
                # no voice chat in the group -> every other assistant would fail the same way
                if "noactivegroupcall" in low or "no active group call" in low or "groupcallnotfound" in low:
                    break
        if not joined:
            self.active_clients.pop(chat_id, None)
            raise AssistantErr(
                f"VC Error: {last_error} - (Please check if Voice Chat is turned on in the group)"
            )

        self.active_clients[chat_id] = [assistant_to_join]  # always remember the assistant that really joined
        await add_active_chat(chat_id)
        await music_on(chat_id)
        if video: await add_active_video_chat(chat_id)
        asyncio.create_task(vc_log("ASSISTANT JOINED VC", chat_id))

        if await is_autoend(chat_id):
            counter[chat_id] = {}
            try:
                users = len(await assistant_to_join.get_participants(chat_id))
                if users == 1:
                    autoend[chat_id] = datetime.now() + timedelta(minutes=1)
            except: pass

    # ------------------------------------------------------------------
    # CHANGE STREAM  (song ended / skipped)
    # ------------------------------------------------------------------
    async def _leave_after_queue_end(self, client, chat_id, popped, autoplay_enabled):
        try:
            bot_to_send = popped.get("client", app) if popped else app
            original_chat_id = popped.get("chat_id", chat_id) if popped else chat_id
            await bot_to_send.send_message(
                original_chat_id,
                build_queue_end_card(autoplay_enabled),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            LOGGER(__name__).error(f"Failed to send leave message: {e}")
        await _clear_(chat_id)
        discard_prepared(chat_id)
        self.active_clients.pop(chat_id, None)
        asyncio.create_task(vc_log("QUEUE ENDED - ASSISTANT LEFT", chat_id))
        try: await client.leave_call(chat_id)
        except Exception: pass

    async def change_stream(self, client, chat_id, _depth: int = 0):
        active_assistants = await self.get_active_clients(chat_id)
        client = active_assistants[0] if active_assistants else client

        check = db.get(chat_id)
        popped = None
        loop = await get_loop(chat_id)

        try:
            if loop == 0:
                if check: popped = check.pop(0)
            else:
                loop = loop - 1
                await set_loop(chat_id, loop)

            if popped: await auto_clean(popped)

            if not db.get(chat_id):
                autoplay_enabled = await is_autoplay_on(chat_id)
                if popped and autoplay_enabled:
                    started = await self.autoplay_start(
                        chat_id,
                        popped.get("chat_id", chat_id),
                        popped.get("title"),
                        popped.get("vidid"),
                        client=client,
                        bot_client=popped.get("client", app),
                    )
                    if started:
                        return
                return await self._leave_after_queue_end(client, chat_id, popped, autoplay_enabled)
        except Exception as e:
            LOGGER(__name__).error(f"❌ Error inside change_stream execution framework: {e}")
            await _clear_(chat_id)
            self.active_clients.pop(chat_id, None)
            try: await client.leave_call(chat_id)
            except Exception: pass
            return

        if not db.get(chat_id):
            return

        track = db[chat_id][0]
        queued = str(track["file"])
        original_chat_id = track["chat_id"]
        streamtype = track["streamtype"]
        videoid = track["vidid"]
        chat_client = track.get("client") or app

        track["played"] = 0
        if track.get("old_dur"):
            track["dur"] = track["old_dur"]
            track["seconds"] = track["old_second"]
            track["speed_path"] = None
            track["speed"] = 1.0
        video = str(streamtype) == "video"

        try:
            language = await get_lang(chat_id)
            _ = get_string(language)
        except Exception:
            _ = get_string("en")

        title = str(track.get("title") or "Unknown Title").title()
        raw_user = track.get("by")
        user = str(raw_user) if raw_user and str(raw_user).strip() else "Unknown User"
        duration_str = track.get("dur", "0:00")
        yt_link = f"https://t.me/{getattr(chat_client.me, 'username', None) or app.username}?start=info_{videoid}"

        async def _skip_failed(reason: str):
            LOGGER(__name__).warning(f"Skipping broken track in {chat_id}: {reason}")
            if _depth >= 4:
                try:
                    await chat_client.send_message(original_chat_id, _["call_6"])
                except Exception:
                    pass
                return await self.stop_stream(chat_id)
            try:
                await chat_client.send_message(original_chat_id, _["call_6"])
            except Exception:
                pass
            await asyncio.sleep(1)
            return await self.change_stream(client, chat_id, _depth + 1)

        # ---------------- live ----------------
        if queued.startswith("live_"):
            n, link = await YouTube.video(videoid, True)
            if n == 0:
                return await _skip_failed("live link")
            try: await self._safe_change_stream(client, chat_id, link, video)
            except Exception as e: return await _skip_failed(str(e))
            caption = build_play_card(yt_link, title, "LIVE", user, chat_id, await is_autoplay_on(chat_id), "YouTube Live")
            await self._announce(chat_client, chat_id, original_chat_id, track, caption, "tg", config.STREAM_IMG_URL)
            return await self.on_track_started(chat_id)

        # ---------------- youtube (queued lazily, or file got cleaned) ----------------
        is_yt = self._is_yt_id(videoid) and videoid not in ("telegram", "soundcloud")
        need_download = queued.startswith("vid_") or (
            is_yt and not queued.startswith(("http", "index_")) and not os.path.exists(queued)
        )
        if need_download:
            mystic = None
            if queued.startswith("vid_"):
                try: mystic = await chat_client.send_message(original_chat_id, _["call_7"])
                except Exception: mystic = None
            track["file"] = f"vid_{videoid}"
            file_path = None
            for _try in range(2):
                try:
                    file_path = await resolve_track_file(track, video)
                except Exception as e:
                    LOGGER(__name__).warning(f"download attempt {_try + 1} failed: {e}")
                if file_path:
                    break
                await asyncio.sleep(1)
            if mystic:
                try: await mystic.delete()
                except Exception: pass
            if not file_path:
                return await _skip_failed("download failed")
            try: await self._safe_change_stream(client, chat_id, file_path, video)
            except Exception as e: return await _skip_failed(str(e))
            caption = build_play_card(yt_link, title, duration_str, user, chat_id, await is_autoplay_on(chat_id))
            await self._announce(chat_client, chat_id, original_chat_id, track, caption, "stream")
            return await self.on_track_started(chat_id)

        # ---------------- index / m3u8 ----------------
        if queued.startswith("index_"):
            try: await self._safe_change_stream(client, chat_id, videoid, video)
            except Exception as e: return await _skip_failed(str(e))
            caption = build_play_card(None, "Index / M3u8 Stream", "LIVE", user, chat_id, await is_autoplay_on(chat_id), "Stream Link")
            await self._announce(chat_client, chat_id, original_chat_id, track, caption, "tg", config.STREAM_IMG_URL)
            return await self.on_track_started(chat_id)

        # ---------------- telegram / soundcloud / spotify / apple / already-downloaded file ----------------
        try: await self._safe_change_stream(client, chat_id, queued, video)
        except Exception as e: return await _skip_failed(str(e))

        if videoid == "telegram":
            img = config.TELEGRAM_VIDEO_URL if video else config.TELEGRAM_AUDIO_URL
            caption = build_play_card(config.SUPPORT_CHAT, title, duration_str, user, chat_id, await is_autoplay_on(chat_id), "Telegram")
            await self._announce(chat_client, chat_id, original_chat_id, track, caption, "tg", img)
        elif videoid in ("soundcloud", "spotify", "apple", "jiosaavn"):
            caption = build_play_card(config.SUPPORT_CHAT, title, duration_str, user, chat_id, await is_autoplay_on(chat_id), str(videoid).title())
            await self._announce(chat_client, chat_id, original_chat_id, track, caption, "tg", config.SOUNCLOUD_IMG_URL)
        else:
            caption = build_play_card(yt_link, title, duration_str, user, chat_id, await is_autoplay_on(chat_id))
            await self._announce(chat_client, chat_id, original_chat_id, track, caption, "stream")
        await self.on_track_started(chat_id)

    async def ping(self):
        pings = []
        if getattr(config, "STRING1", None): pings.append(self.one.ping)
        if getattr(config, "STRING2", None): pings.append(self.two.ping)
        if getattr(config, "STRING3", None): pings.append(self.three.ping)
        if getattr(config, "STRING4", None): pings.append(self.four.ping)
        if getattr(config, "STRING5", None): pings.append(self.five.ping)
        return pings

    async def start(self):
        LOGGER(__name__).info("Starting PyTgCalls Client...\n")
        for client in [self.one, self.two, self.three, self.four, self.five]:
            if not client:
                continue
            try:
                await client.start()
                self._started_calls.append(client)
            except Exception:
                LOGGER(__name__).exception("Failed to start one PyTgCalls assistant")
        if not self._started_calls:
            raise RuntimeError("No PyTgCalls assistant could be started")

    async def stop(self):
        for client in reversed(self._started_calls):
            try:
                await client.stop()
            except Exception:
                LOGGER(__name__).warning(
                    "Failed to stop one PyTgCalls assistant", exc_info=True
                )
        self._started_calls.clear()

ANJALI = Call()
