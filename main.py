"""
main.py — Lumo entry point.

Architecture:
  • Telethon userbot  — joins channels, receives posts
  • Aiogram bot       — handles user commands, sends notifications
  • processor.py      — AI pipeline (relevance + extraction)
  • tracker.py        — aiosqlite database layer

Hot-path optimisation (100+ users):
  For every incoming post:
    1. Load all subscriptions for that channel (one DB query).
    2. Skip if zero subscribers.
    3. Spam pre-filter (regex, free).
    4. Group subscriptions by (goal, frozenset(keywords)).
    5. For each unique group → ONE Groq relevance + extraction call.
    6. Notify all users in that group with the cached result.
"""

import os
import json
import asyncio
import re
from itertools import groupby
from dotenv import load_dotenv

load_dotenv()

from telethon import TelegramClient, events
from telethon.utils import get_peer_id
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.errors import FloodWaitError, ChannelPrivateError, UsernameNotOccupiedError
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest

from utils import qr_auth
from tracker import (
    init_db, upsert_user,
    add_subscription,
    get_subscribers_for_channel,
    archive_opportunity,
    search_archive,
    set_user_criteria,
)
from processor import (
    GroqService,
    GroqAPIError,
    process_post,
    is_spam,
    extract_post_text,
    should_process_post,
    format_opportunity_plain,
)
from bot import bot, start_bot, pending_requests, register_search_handler

# ─── Env ──────────────────────────────────────────────────────────────────────

API_ID = int(os.getenv("CLIENT_API"))
API_HASH = os.getenv("CLIENT_API_HASH")
SESSION_PATH = os.getenv("SESSION_PATH", os.path.join(os.path.dirname(__file__), "bot_session"))

# ─── Helpers ─────────────────────────────────────────────────────────────────

def parse_llm_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return {}


def build_post_link(channel_username: str | None, channel_tg_id: int, message_id: int) -> str | None:
    """Public post URL for inline button (username or private c/ link)."""
    if channel_username:
        return f"https://t.me/{channel_username}/{message_id}"
    cid = str(channel_tg_id)
    if cid.startswith("-100"):
        cid = cid[4:]
    elif cid.startswith("-"):
        cid = cid[1:]
    if cid.isdigit():
        return f"https://t.me/c/{cid}/{message_id}"
    return None


def _notification_keyboard(
    gcal_link: str | None,
    post_link: str | None,
) -> InlineKeyboardMarkup | None:
    row = []
    if post_link:
        row.append(InlineKeyboardButton(text="🔗 Открыть пост", url=post_link))
    if gcal_link:
        row.append(InlineKeyboardButton(text="📅 В календарь", url=gcal_link))
    if not row:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[row])


JOIN_ERROR_USER_MSG = (
    "❌ Не удалось отслеживать канал. "
    "Возможно, он приватный или превышен лимит Телеграма. Попробуй позже."
)

_groq_warned_users: set[int] = set()


async def _send_user_notification(
    user_id: int,
    html_message: str,
    reply_markup: InlineKeyboardMarkup | None,
    structured: dict | None,
    channel_username: str,
    reason: str,
) -> None:
    """Send notification; retry as plain text if Telegram rejects HTML entities."""
    try:
        await bot.send_message(
            user_id,
            html_message,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
        print(f"[MAIN] Notified user {user_id} from @{channel_username}")
    except TelegramBadRequest as e:
        err = str(e).lower()
        if "parse entities" in err or "can't parse" in err:
            plain = format_opportunity_plain(
                structured or {},
                channel_username,
                reason,
            )
            await bot.send_message(
                user_id,
                plain,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            print(f"[MAIN] Notified user {user_id} (plain fallback)")
        else:
            raise


async def _join_channel_safe(client: TelegramClient, entity, user_id: int, handle: str) -> bool:
    """
    Join channel with Telethon error handling.
    Returns True on success; sends a user-facing message and returns False on failure.
    """
    try:
        await client(JoinChannelRequest(entity))
        return True
    except FloodWaitError as e:
        print(f"[MAIN] FloodWait @{handle}: {e.seconds}s")
        await bot.send_message(
            user_id,
            f"⏳ Telegram просит подождать {e.seconds} сек. Повтори подписку чуть позже.",
        )
        return False
    except ChannelPrivateError:
        print(f"[MAIN] ChannelPrivate @{handle}")
        await bot.send_message(user_id, JOIN_ERROR_USER_MSG)
        return False
    except UsernameNotOccupiedError:
        print(f"[MAIN] UsernameNotOccupied @{handle}")
        await bot.send_message(
            user_id,
            f"❌ Канал @{handle} не найден. Проверь написание username.",
        )
        return False
    except Exception as e:
        print(f"[MAIN] Join error @{handle}: {e}")
        await bot.send_message(user_id, JOIN_ERROR_USER_MSG)
        return False


# ─── Direct subscription (no LLM) ─────────────────────────────────────────────

async def process_direct_subscription(
    client: TelegramClient,
    user_id: int,
    username: str,
    channel_handle: str,
    goal: str,
    keywords: list,
    cadence: str = "immediate",
    audience_criteria: str | None = None,
) -> None:
    """Add subscription from inline template — skips augment/classify."""
    print(f"[MAIN] Direct subscribe {user_id} -> @{channel_handle} keywords={keywords}")

    await upsert_user(user_id, username)
    if audience_criteria:
        await set_user_criteria(user_id, audience_criteria)
    handle = channel_handle.lstrip("@").split("/")[-1]

    try:
        entity = await client.get_entity(handle)
    except UsernameNotOccupiedError:
        await bot.send_message(
            user_id,
            f"❌ Канал @{handle} не найден. Проверь написание username.",
        )
        return
    except Exception as e:
        print(f"[MAIN] get_entity @{handle}: {e}")
        await bot.send_message(user_id, JOIN_ERROR_USER_MSG)
        return

    if not await _join_channel_safe(client, entity, user_id, handle):
        return

    tg_id = get_peer_id(entity)
    added = await add_subscription(user_id, handle, tg_id, goal, keywords, cadence)

    kw_preview = ", ".join(keywords[:5])
    if len(keywords) > 5:
        kw_preview += "…"

    criteria_note = ""
    if audience_criteria:
        criteria_note = f"\n👤 *Критерии:* {audience_criteria[:120]}{'…' if len(audience_criteria) > 120 else ''}"

    if added:
        await bot.send_message(
            user_id,
            f"✅ *@{handle}* добавлен в отслеживание!\n\n"
            f"🎯 Фильтр: {goal}\n"
            f"🔑 Ключевые слова: {kw_preview}"
            f"{criteria_note}\n\n"
            "Как только появится подходящий пост — пришлю уведомление 🔔",
            parse_mode="Markdown",
        )
    else:
        await bot.send_message(
            user_id,
            f"⚠️ *@{handle}* уже в списке отслеживания.\n"
            "Используй /list чтобы посмотреть каналы.",
            parse_mode="Markdown",
        )

    await asyncio.sleep(1)


# ─── User request handler (LLM path) ──────────────────────────────────────────

async def process_user_request(
    client: TelegramClient,
    groq: GroqService,
    user_id: int,
    username: str,
    request_text: str,
) -> None:
    print(f"[MAIN] LLM request from {user_id}: {request_text!r}")

    await upsert_user(user_id, username)

    try:
        augmented = await groq.augment(request_text)
        raw = await groq.classify_intent(augmented)
        data = parse_llm_json(raw)
    except Exception as e:
        print(f"[MAIN] Groq error: {e}")
        await bot.send_message(user_id, "❌ Ошибка связи с ИИ. Попробуй позже.")
        return

    if not data:
        await bot.send_message(user_id, "❌ Не удалось разобрать запрос.")
        return

    if data.get("ambiguous") and data.get("clarification_needed"):
        await bot.send_message(user_id, data["clarification_needed"])
        return

    channels = data.get("channels", [])
    if not channels:
        await bot.send_message(user_id, "❌ Не указаны каналы для мониторинга.")
        return

    goal = data.get("goal", "monitor")
    keywords = data.get("keywords", [])
    cadence = data.get("cadence", "immediate")

    results = []
    for ch in channels:
        handle = ch.lstrip("@").split("/")[-1]
        try:
            entity = await client.get_entity(handle)
        except UsernameNotOccupiedError:
            results.append(f"❓ @{handle} — канал не найден")
            continue
        except Exception as e:
            results.append(f"❌ @{handle} — ошибка: {e}")
            continue

        if not await _join_channel_safe(client, entity, user_id, handle):
            results.append(f"❌ @{handle} — не удалось подключиться")
            continue

        tg_id = get_peer_id(entity)
        added = await add_subscription(user_id, handle, tg_id, goal, keywords, cadence)
        if added:
            results.append(f"✅ @{handle} — добавлен")
        else:
            results.append(f"⚠️ @{handle} — уже отслеживается")

        await asyncio.sleep(1)

    await bot.send_message(user_id, "Настройка завершена:\n\n" + "\n".join(results))


# ─── Pending request poller ───────────────────────────────────────────────────

async def poll_pending_requests(client: TelegramClient, groq: GroqService) -> None:
    while True:
        for user_id in list(pending_requests.keys()):
            req = pending_requests.pop(user_id, None)
            if not req:
                continue

            if req.get("type") == "direct":
                await process_direct_subscription(
                    client,
                    user_id,
                    req.get("username", ""),
                    req["channel_handle"],
                    req.get("goal", "monitor"),
                    req.get("keywords", []),
                    audience_criteria=req.get("audience_criteria"),
                )
            else:
                await process_user_request(
                    client, groq,
                    user_id,
                    req.get("username", ""),
                    req["text"],
                )
        await asyncio.sleep(2)


# ─── Post handler (hot path) ──────────────────────────────────────────────────

async def _warn_groq_failure(user_ids: list[int], err: GroqAPIError) -> None:
    if err.status_code != 401:
        return
    for uid in user_ids:
        if uid in _groq_warned_users:
            continue
        _groq_warned_users.add(uid)
        try:
            await bot.send_message(
                uid,
                "⚠️ <b>ИИ временно недоступен</b> (неверный GROQ_API_KEY в .env).\n\n"
                "Посты с подходящими ключевыми словами всё равно придут в упрощённом виде.\n"
                "Обнови ключ на https://console.groq.com и перезапусти бота.",
                parse_mode="HTML",
            )
        except Exception:
            pass


async def handle_channel_post(event, groq: GroqService) -> None:
    """
    Called for every incoming channel message.
    One DB read → group subscribers → one Groq call per unique (goal, keywords, audience) group.
    """
    text = extract_post_text(event)
    channel_tg_id = event.chat_id
    subs = await get_subscribers_for_channel(channel_tg_id)
    if not subs:
        return

    all_keywords = [kw for s in subs for kw in s.keywords]
    if not should_process_post(text, all_keywords):
        print(f"[MAIN] Post too short/skipped ch={channel_tg_id} len={len(text)}")
        return

    if is_spam(text):
        print(f"[MAIN] Spam skipped in channel {channel_tg_id}")
        return

    channel_username = subs[0].channel_username
    post_link = build_post_link(channel_username, channel_tg_id, event.id)
    print(f"[MAIN] Processing post ch=@{channel_username} id={event.id} len={len(text)}")

    def group_key(s):
        return (s.goal, tuple(sorted(s.keywords)), s.audience_criteria or "")

    sorted_subs = sorted(subs, key=group_key)
    archived = False

    for key, group in groupby(sorted_subs, key=group_key):
        goal, kw_tuple, audience = key
        group_list = list(group)

        try:
            result = await process_post(
                groq,
                text,
                channel_username,
                goal,
                list(kw_tuple),
                audience_criteria=audience,
            )
        except GroqAPIError as e:
            print(f"[MAIN] GroqAPIError: {e}")
            await _warn_groq_failure([s.user_id for s in group_list], e)
            continue
        except Exception as e:
            print(f"[MAIN] process_post crashed: {e}")
            continue

        if not result.is_relevant:
            print(f"[MAIN] Not relevant for @{channel_username} goal={goal!r}")
            continue

        if not archived:
            await archive_opportunity(channel_tg_id, text, result.structured)
            archived = True

        reply_markup = _notification_keyboard(result.gcal_link, post_link)

        for sub in group_list:
            try:
                await _send_user_notification(
                    sub.user_id,
                    result.message,
                    reply_markup,
                    result.structured,
                    channel_username,
                    result.reason,
                )
            except Exception as e:
                print(f"[MAIN] Failed to notify {sub.user_id}: {e}")


# ─── /search command ──────────────────────────────────────────────────────────

async def search_command_handler(user_id: int, query: str) -> None:
    if not query.strip():
        await bot.send_message(user_id, "Использование: /search [запрос]")
        return

    results = await search_archive(query.strip(), limit=5)
    if not results:
        await bot.send_message(user_id, f"🔍 По запросу «{query}» ничего не найдено.")
        return

    lines = [f"🔍 Результаты по «{query}»:\n"]
    for opp in results:
        s = opp.structured or {}
        title = s.get("title") or opp.raw_text[:60] + "…"
        deadline = s.get("deadline") or "—"
        link = s.get("link") or ""
        ts = opp.timestamp[:10]
        lines.append(f"• *{title}*\n  Дедлайн: `{deadline}` | {ts}\n  {link}\n")

    await bot.send_message(user_id, "\n".join(lines), parse_mode="Markdown")


# ─── Safe task wrapper ────────────────────────────────────────────────────────

async def safe_task(name: str, coro) -> None:
    try:
        await coro
    except asyncio.CancelledError:
        print(f"[MAIN] Task '{name}' cancelled")
        raise
    except Exception as e:
        print(f"[MAIN] Task '{name}' crashed: {e}")
        raise


# ─── Entry point ──────────────────────────────────────────────────────────────

async def main() -> None:
    await init_db()

    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    groq = GroqService()

    await client.connect()
    if not await client.is_user_authorized():
        await qr_auth(client)

    print("[MAIN] Userbot authorised.")

    key = (os.getenv("GROQ_API_KEY") or "").strip()
    if not key:
        print("[MAIN] WARNING: GROQ_API_KEY пустой в .env")
    else:
        print(f"[MAIN] Groq key loaded: {GroqService.api_key_fingerprint()}")
        try:
            await groq.check_relevance("test хакатон", "monitor", ["хакатон"])
            print("[MAIN] Groq API OK")
        except GroqAPIError as e:
            print(
                f"[MAIN] WARNING: Groq отклонил ключ ({e.status_code} Invalid API Key). "
                "Создай НОВЫЙ ключ на https://console.groq.com/keys — текущий недействителен. "
                "Уведомления идут по ключевым словам без ИИ."
            )

    register_search_handler(search_command_handler)

    @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_channel))
    async def _on_post(event):
        try:
            await handle_channel_post(event, groq)
        except Exception as e:
            print(f"[MAIN] Unhandled post error: {e}")

    print("[MAIN] Lumo is running 🚀")

    try:
        await asyncio.gather(
            safe_task("bot", start_bot()),
            safe_task("poll", poll_pending_requests(client, groq)),
            safe_task("userbot", client.run_until_disconnected()),
        )
    finally:
        await groq.close()
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
