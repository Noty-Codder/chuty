"""Tiny admin check that does NOT require an active stream (used by settings panels)."""
from pyrogram.enums import ChatMemberStatus, ChatType

from SHIVMUSIC.misc import SUDOERS


async def is_chat_admin(client, chat_id: int, user_id: int) -> bool:
    if user_id in SUDOERS:
        return True
    if user_id in (777000, 1087968824):  # service / anonymous admin
        return True
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)
    except Exception:
        return False


def is_group(message) -> bool:
    return message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
