"""
Central HTML / "tablet" (blockquote) builders for every card the bot sends.

Telegram HTML rules respected here
  * only <b> <i> <u> <s> <code> <a> <tg-emoji> <blockquote> <blockquote expandable>
  * <blockquote> never nested inside another <blockquote>
  * <pre>/<code> are NOT placed inside a blockquote (keeps it parse-safe everywhere)
  * every dynamic value is html-escaped
Photo captions are limited to 1024 visible chars, so play cards stay compact.
"""
from html import escape as _esc

import config

# ---------- premium emoji helper ----------
E = {
    "play": ("5850346984501680054", "▶️"),
    "title": ("6100424015111787987", "📌"),
    "time": ("5267421370114914946", "⏱"),
    "user": ("5258362837411045098", "👤"),
    "music": ("5409025823388741707", "🎵"),
    "eq": ("5217933090483098080", "🎚"),
    "vol": ("5258389041006518073", "🔊"),
    "headset": ("6082387600599944892", "🎧"),
    "spark": ("6172312314423808834", "✨"),
    "gem": ("6271537028307881531", "💎"),
    "bot": ("5355051922862653659", "🤖"),
    "ask": ("5188540541922480562", "❓"),
    "ok": ("6280269890821558384", "✅"),
    "bad": ("6271611232457855630", "❌"),
    "warn": ("6102938383456146362", "⚠️"),
    "loop": ("6030657343744644592", "🔁"),
    "star": ("5409368076447657845", "🌟"),
    "fast": ("6100220081474639964", "⚡"),
    "red": ("5318840353510408444", "🔴"),
    "green": ("6113685078825505075", "🟢"),
}


def em(name: str) -> str:
    cid, fallback = E[name]
    return f'<tg-emoji emoji-id="{cid}">{fallback}</tg-emoji>'


def esc(text) -> str:
    return _esc(str(text if text is not None else ""), quote=False)


def clip(text, n: int = 38) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def quote(body: str, expandable: bool = False) -> str:
    tag = "blockquote expandable" if expandable else "blockquote"
    return f"<{tag}>{body}</{tag}>"


BRAND_LEFT = getattr(config, "BRAND_LEFT", "BETA BOT HUB")
BRAND_RIGHT = getattr(config, "BRAND_RIGHT", "THE SHIV")


def _footer() -> str:
    return ""  # branding removed on request


class CardText(str):
    """HTML card text that also carries `.info` = {"title", "rows"} for the native table (rich message)."""

    info = None


def _with_info(text: str, title: str, rows: list) -> "CardText":
    c = CardText(text.rstrip("\n"))
    c.info = {"title": title, "rows": rows}
    return c


def _plain_rows(chat_id, autoplay=None) -> list:
    out = []
    if chat_id is None:
        return out
    try:
        from SHIVMUSIC.utils.equalizer import get_chat_eq, get_chat_volume, get_eq_name

        out.append(["🎚 Mood / EQ", get_eq_name(get_chat_eq(chat_id))])
        out.append(["🔊 Volume", f"{get_chat_volume(chat_id)}%"])
    except Exception:
        pass
    if autoplay is not None:
        out.append(["🔁 Autoplay", "ON" if autoplay else "OFF"])
    return out


# =====================================================================
# WELCOME  (start in PM / group / when bot is added)
# =====================================================================
def build_welcome_html(user_mention: str, bot_mention: str, bot_username: str) -> str:
    support = getattr(config, "SUPPORT_CHAT", "https://t.me/betabot_support")
    channel = getattr(config, "SUPPORT_CHANNEL", "https://t.me/betabot_hub")
    head = quote(f"{em('star')} <b>HEY {user_mention}, WELCOME ABOARD!</b> {em('headset')}")
    intro = quote(
        f"{em('bot')} <b>I AM {bot_mention} — YOUR PERSONAL MUSIC COMPANION, LIVE 24/7.</b> {em('headset')}"
    )
    features = quote(
        f"<b>✦ KEY FEATURES ✦</b>\n"
        f"<b>FEATURE</b>  │  <b>DETAILS</b>\n"
        f"{em('music')} <b>Streaming</b>  │  <i>High-quality, zero lag</i>\n"
        f"{em('fast')} <b>Speed</b>  │  <i>Instant response, always on</i>\n"
        f"{em('eq')} <b>Equalizer</b>  │  <i>13 pro presets + volume</i>\n"
        f"{em('loop')} <b>Autoplay</b>  │  <i>Spotify-style endless radio</i>\n"
        f"{em('gem')} <b>VIP Care</b>  │  <i>Priority support, smooth vibes</i>",
        expandable=True,
    )
    why = quote(
        f"<b>✧ WHY CHOOSE {bot_mention}? ✧</b>\n"
        f"{em('music')} <b>USER-FRIENDLY COMMANDS FOR ANYONE.</b>\n"
        f"{em('vol')} <b>CRYSTAL-CLEAR AUDIO, EVERY TIME.</b>\n"
        f"{em('gem')} <b>TRUSTED BY THOUSANDS OF GROUPS.</b>",
        expandable=True,
    )
    ready = quote(f"{em('spark')} <b>READY FOR THE BEST MUSIC EXPERIENCE?</b>")
    invite = (
        f"{em('spark')} <b><i>INVITE ME NOW AND ENJOY THE VIBE!</i></b> 🎉\n"
        f'<b><a href="{support}">Support™</a>  ✦  <a href="{channel}">Updates™</a></b>'
    )
    return f"{head}\n{intro}\n{features}\n{why}\n{ready}\n{invite}"


# =====================================================================
# FONT / STYLE HELPERS  (small-caps + bold-script, the "special" look)
# =====================================================================
_SC = dict(zip(
    "abcdefghijklmnopqrstuvwxyz",
    "ᴀʙᴄᴅᴇғɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢ",
))


def sc(text) -> str:
    """tiny small-caps:  Started Streaming -> sᴛᴀʀᴛᴇᴅ sᴛʀᴇᴀᴍɪɴɢ"""
    return "".join(_SC.get(ch, _SC.get(ch.lower(), ch)) if ch.isascii() and ch.isalpha() else ch for ch in str(text))


def script_bold(text) -> str:
    """plain ASCII letters -> bold script (Kutty -> 𝓚𝓾𝓽𝓽𝔂). Already-fancy names are left alone."""
    out = []
    for ch in str(text):
        o = ord(ch)
        if 65 <= o <= 90:
            out.append(chr(0x1D4D0 + o - 65))
        elif 97 <= o <= 122:
            out.append(chr(0x1D4EA + o - 97))
        else:
            out.append(ch)
    return "".join(out)


def _is_emoji_char(ch: str) -> bool:
    o = ord(ch)
    return o >= 0x1F000 or 0x2600 <= o <= 0x27BF or 0x2B00 <= o <= 0x2BFF


def fancy_name(name, limit: int = 24) -> str:
    """Requester name for the card: 🧸 + bold-script (keeps names that are already styled / have emoji)."""
    name = " ".join(str(name or "Unknown").split())
    if len(name) > limit:
        name = name[: limit - 1] + "…"
    if all(ord(c) < 0x250 for c in name):  # plain latin text -> style it
        name = script_bold(name)
    if not name or not _is_emoji_char(name[0]):
        name = "🧸 " + name
    return esc(name)


def fancy_duration(duration) -> str:
    d = str(duration or "").strip()
    if not d or d.lower() in ("none", "unknown"):
        return esc(sc("unknown"))
    if d.lower() == "live":
        return esc(sc("live"))
    parts = d.split(":")
    if len(parts) >= 3:
        return f"{esc(d)} {sc('hours')}"
    return f"{esc(d)} {sc('minutes')}"


_BULLET = "⊱⋆⊳"


def _line(label: str, value: str) -> str:
    return f"{_BULLET} {sc(label)} : {value}"


def _card(heading: str, lines: list) -> "CardText":
    body = f"➲ {sc(heading)} |\n" + "\n".join(lines)
    return CardText(quote(body))


# =====================================================================
# PLAY CARD  (now playing)
# =====================================================================
def _audio_rows(chat_id) -> str:
    try:
        from SHIVMUSIC.utils.equalizer import get_chat_eq, get_chat_volume, get_eq_name

        return (
            f"{em('eq')} <b>MOOD / EQ</b>  │  <code>{esc(get_eq_name(get_chat_eq(chat_id)))}</code>\n"
            f"{em('vol')} <b>VOLUME</b>  │  <code>{esc(get_chat_volume(chat_id))}%</code>"
        )
    except Exception:
        return ""


def _sync_autoplay_flag(chat_id, autoplay):
    """remember the autoplay state so the player buttons can show ON / OFF without a DB call"""
    if chat_id is None or autoplay is None:
        return
    try:
        from SHIVMUSIC.utils.inline.play import set_autoplay_flag

        set_autoplay_flag(chat_id, bool(autoplay))
    except Exception:
        pass


def build_play_card(
    link,
    title,
    duration,
    by,
    chat_id=None,
    autoplay: bool = False,
    source: str = "YouTube",
    heading: str = "Started Streaming",
) -> str:
    """➲ sᴛᴀʀᴛᴇᴅ sᴛʀᴇᴀᴍɪɴɢ |  — one clean blockquote card."""
    _sync_autoplay_flag(chat_id, autoplay)
    safe_title = esc(clip(title, 60))
    title_html = f'<a href="{esc(link)}">{safe_title}</a>' if link else safe_title
    return _card(
        heading,
        [
            f"{_BULLET} {sc('title')} : {title_html}",
            f"{_BULLET} {sc('duration')} : {fancy_duration(duration)}",
            f"{_BULLET} {sc('requested by')} : {fancy_name(by)}",
        ],
    )


def build_queue_card(position, title, duration, by, chat_id=None) -> str:
    return _card(
        "Added to queue",
        [
            f"{_BULLET} {sc('position')} : #{esc(position)}",
            f"{_BULLET} {sc('title')} : {esc(clip(title, 60))}",
            f"{_BULLET} {sc('duration')} : {fancy_duration(duration)}",
            f"{_BULLET} {sc('requested by')} : {fancy_name(by)}",
        ],
    )


def build_autoplay_card(title, duration, vibe: str = "Similar", chat_id=None, link=None) -> str:
    _sync_autoplay_flag(chat_id, True)
    safe_title = esc(clip(title, 60))
    title_html = f'<a href="{esc(link)}">{safe_title}</a>' if link else safe_title
    return _card(
        "Autoplay started",
        [
            f"{_BULLET} {sc('title')} : {title_html}",
            f"{_BULLET} {sc('duration')} : {fancy_duration(duration)}",
            f"{_BULLET} {sc('vibe')} : {esc(sc(vibe))}",
            f"{_BULLET} {sc('requested by')} : 🔁 {esc(sc('autoplay radio'))}",
        ],
    )


def build_queue_end_card(autoplay_on: bool) -> str:
    state = sc("on, but no fresh track found") if autoplay_on else sc("off")
    return _card(
        "Queue ended",
        [
            f"{_BULLET} {sc('autoplay')} : {esc(state)}",
            f"{_BULLET} {sc('status')} : {esc(sc('leaving the voice chat, thanks for listening'))} ❤️",
        ],
    )


# =====================================================================
# SETTINGS PANELS  (autoplay / thumbnail)
# =====================================================================
def build_autoplay_panel(enabled: bool, style: str) -> str:
    st = f"{em('green')} <b>ENABLED</b>" if enabled else f"{em('red')} <b>DISABLED</b>"
    styles = {
        "similar": "Songs YouTube Mix thinks you'll love next",
        "artist": "More tracks from the same artist / channel",
        "discover": "Fresh tracks, different artists, new vibes",
    }
    lines = []
    for key, desc in styles.items():
        mark = "●" if key == style else "○"
        lines.append(f"{mark} <b>{key.upper()}</b> — <i>{desc}</i>")
    return (
        quote(f"{em('loop')} <b>✦ AUTOPLAY SETTINGS ✦</b>")
        + "\n"
        + quote(f"<b>STATUS</b>  │  {st}\n<b>STYLE</b>  │  <b>{esc(style.upper())}</b>")
        + "\n"
        + quote("<b>✦ AVAILABLE STYLES ✦</b>\n" + "\n".join(lines), expandable=True)
        + "\n"
        + _footer()
    )


def build_thumb_panel(enabled: bool) -> str:
    st = f"{em('green')} <b>ON</b>" if enabled else f"{em('red')} <b>OFF</b>"
    return (
        quote(f"{em('title')} <b>✦ THUMBNAIL SETTINGS ✦</b>")
        + "\n"
        + quote(
            f"<b>STATUS</b>  │  {st}\n"
            "<i>ON</i> — now-playing card comes with a custom thumbnail.\n"
            "<i>OFF</i> — clean text card only (faster, no image)."
        )
        + "\n"
        + _footer()
    )


# =====================================================================
# VC LOGGER
# =====================================================================
def build_vc_log(event: str, chat_title, chat_id, bot_mention, extra: str = "", chat_link=None) -> str:
    title = esc(clip(chat_title, 40))
    if chat_link:
        title = f'<a href="{esc(chat_link)}">{title}</a>'
    body = (
        f"<b>EVENT</b>  │  <b>{esc(event)}</b>\n"
        f"<b>BOT</b>  │  {bot_mention}\n"
        f"<b>CHAT</b>  │  {title} [<code>{esc(chat_id)}</code>]"
    )
    if extra:
        body += "\n" + extra
    return quote(f"{em('headset')} <b>✦ VC LOG ✦</b>") + "\n" + quote(body)
