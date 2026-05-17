"""
bot.py — Aiogram bot (user-facing side).
All heavy logic lives in main.py / processor.py / tracker.py.
"""

import os
import re
import asyncio
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.filters import CommandStart, Command

from tracker import list_subscriptions, remove_subscription, get_user_criteria, set_user_criteria
from processor import AUDIENCE_SCHOOL

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ─── Reply menu labels ────────────────────────────────────────────────────────

BTN_TRACK = "🔍 Отслеживать канал"
BTN_LIST = "📋 Мои подписки"
BTN_HELP = "📖 Помощь"
BTN_CRITERIA = "👤 Мои критерии"

main_menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_TRACK)],
        [KeyboardButton(text=BTN_LIST), KeyboardButton(text=BTN_CRITERIA)],
        [KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
)

# Pending LLM requests: { user_id: {"text": str, "username": str} }
pending_requests: dict = {}

# Channel awaiting template choice: { user_id: channel_handle }
pending_channel_choice: dict = {}

# Awaiting custom keywords after template pick: { user_id: channel_handle }
pending_custom_keywords: dict = {}

# Awaiting audience criteria text from user
pending_criteria_input: set = set()

CB_PREFIX = "lumo:"

TEMPLATES = {
    "hack": {
        "label": "🏆 Хакатоны и Гранты",
        "goal": "хакатоны и гранты",
        "keywords": ["хакатон", "грант", "конкурс", "hackathon", "grant"],
    },
    "job": {
        "label": "💼 Стажировки и Вакансии",
        "goal": "стажировки и вакансии",
        "keywords": ["стажировка", "вакансия", "internship", "job", "работа"],
    },
    "school": {
        "label": "🎓 Для школьников",
        "goal": "конкурсы и хакатоны для школьников",
        "keywords": [
            "школ", "ученик", "олимпиад", "хакатон", "hackathon",
            "конкурс", "грант", "grant", "k12", "school",
        ],
        "audience_criteria": AUDIENCE_SCHOOL,
    },
}

# Injected by main.py after import to avoid circular dependency
_search_handler = None


def register_search_handler(fn):
    """Called from main.py: register_search_handler(search_command_handler)"""
    global _search_handler
    _search_handler = fn


def extract_channel_handle(text: str) -> str | None:
    """Parse @username or t.me/username from a message."""
    text = text.strip()
    if not text:
        return None

    if text.startswith("@"):
        return text.lstrip("@").split()[0].split("/")[0].split("?")[0]

    m = re.search(r"(?:https?://)?t\.me/([a-zA-Z0-9_]+)", text, re.IGNORECASE)
    if m:
        return m.group(1)

    if " " in text and not text.startswith("@"):
        m = re.search(r"@([a-zA-Z0-9_]+)", text)
        if m:
            return m.group(1)

    return None


def template_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏆 Хакатоны и Гранты", callback_data=f"{CB_PREFIX}hack")],
            [InlineKeyboardButton(text="🎓 Для школьников", callback_data=f"{CB_PREFIX}school")],
            [InlineKeyboardButton(text="💼 Стажировки и Вакансии", callback_data=f"{CB_PREFIX}job")],
            [InlineKeyboardButton(text="🎯 Свои ключевые слова", callback_data=f"{CB_PREFIX}custom")],
        ]
    )


def _queue_direct_subscription(
    user_id: int,
    username: str,
    channel_handle: str,
    goal: str,
    keywords: list,
    audience_criteria: str | None = None,
) -> None:
    pending_requests[user_id] = {
        "type": "direct",
        "channel_handle": channel_handle,
        "goal": goal,
        "keywords": keywords,
        "user_id": user_id,
        "username": username,
        "audience_criteria": audience_criteria,
    }


HELP_TEXT = (
    "📖 *Как работает Lumo:*\n\n"
    "1️⃣ Нажми кнопку *'🔍 Отслеживать канал'* или просто отправь юзернейм "
    "(например, `@astana_hub` или ссылку `t.me/...`)\n"
    "2️⃣ Выбери категорию: *Хакатоны*, *Для школьников*, *Стажировки* или *Свои ключевые слова*.\n"
    "3️⃣ Настрой *👤 Мои критерии* — например: «я школьник, нужны конкурсы для школьников».\n"
    "4️⃣ ИИ будет анализировать посты (включая превью ссылок) и присылать карточки "
    "с дедлайнами и кнопкой в календарь.\n\n"
    "📌 *Команды:*\n"
    "/list — твои каналы\n"
    "/criteria — настроить критерии аудитории\n"
    "/stop @username — отписаться\n"
    "/search [запрос] — поиск по архиву"
)


async def _send_subscriptions_list(message: Message) -> None:
    """Shared logic for /list and the «Мои подписки» menu button."""
    user_id = message.from_user.id
    channels = await list_subscriptions(user_id)

    if not channels:
        await message.answer(
            "У тебя пока нет отслеживаемых каналов.\n\n"
            "Нажми *🔍 Отслеживать канал* или отправь `@username` канала!",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard,
        )
        return

    criteria = await get_user_criteria(user_id)
    lines = ["📋 *Твои подписки:*\n"]
    for ch in channels:
        kw = ", ".join(ch.keywords) if ch.keywords else "все посты"
        lines.append(f"• @{ch.channel_username} — {ch.goal} | 🔑 {kw} | 🕐 {ch.cadence}")
    if criteria:
        lines.append(f"\n👤 *Твои критерии:* {criteria}")

    await message.answer(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard,
    )


# ─── /start ───────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Я *Lumo* — твой автономный AI-скаут для Telegram.\n\n"
        "Я умею круглосуточно мониторить любые каналы, фильтровать спам через ИИ "
        "и мгновенно присылать тебе только важные хакатоны, гранты, стажировки "
        "или кастомные инсайды.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard,
    )


# ─── Reply menu buttons ───────────────────────────────────────────────────────

@dp.message(F.text == BTN_TRACK)
async def menu_track(message: Message):
    await message.answer(
        "🔗 Отправь мне юзернейм канала (например, `@astana_hub`) "
        "или ссылку на него, и мы настроим мониторинг!",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard,
    )


@dp.message(F.text == BTN_LIST)
async def menu_list(message: Message):
    await _send_subscriptions_list(message)


@dp.message(F.text == BTN_HELP)
async def menu_help(message: Message):
    await message.answer(HELP_TEXT, parse_mode="Markdown", reply_markup=main_menu_keyboard)


@dp.message(F.text == BTN_CRITERIA)
async def menu_criteria(message: Message):
    await _prompt_criteria(message)


async def _prompt_criteria(message: Message) -> None:
    user_id = message.from_user.id
    current = await get_user_criteria(user_id)
    pending_criteria_input.add(user_id)
    text = (
        "👤 *Мои критерии* — кто ты и что искать\n\n"
        "Напиши одним сообщением, например:\n"
        "• `Я школьник, нужны олимпиады и хакатоны для школьников`\n"
        "• `Студент 2 курса IT, ищу стажировки`\n"
        "• `Сброс` — убрать критерии\n\n"
    )
    if current:
        text += f"*Сейчас:* {current}"
    else:
        text += "*Сейчас:* не заданы (подходят все подходящие по категории посты)"
    await message.answer(text, parse_mode="Markdown", reply_markup=main_menu_keyboard)


# ─── /list ────────────────────────────────────────────────────────────────────

@dp.message(Command("list"))
async def cmd_list(message: Message):
    await _send_subscriptions_list(message)


# ─── /stop ────────────────────────────────────────────────────────────────────

@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer(
            "Укажи канал: `/stop @username`",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard,
        )
        return

    handle = args[1].lstrip("@")
    user_id = message.from_user.id

    removed = await remove_subscription(user_id, handle)
    if removed:
        await message.answer(
            f"✅ @{handle} удалён из отслеживания.",
            reply_markup=main_menu_keyboard,
        )
    else:
        await message.answer(
            f"@{handle} не найден в твоих каналах.",
            reply_markup=main_menu_keyboard,
        )


# ─── /search ──────────────────────────────────────────────────────────────────

@dp.message(Command("search"))
async def cmd_search(message: Message):
    query = message.text.removeprefix("/search").strip()
    if _search_handler:
        await _search_handler(message.from_user.id, query)
    else:
        await message.answer(
            "🔍 Поиск временно недоступен.",
            reply_markup=main_menu_keyboard,
        )


# ─── /help ────────────────────────────────────────────────────────────────────

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT, parse_mode="Markdown", reply_markup=main_menu_keyboard)


@dp.message(Command("criteria"))
async def cmd_criteria(message: Message):
    await _prompt_criteria(message)


# ─── Callback: template selection ───────────────────────────────────────────────

@dp.callback_query(F.data.startswith(CB_PREFIX))
async def on_template_choice(callback: CallbackQuery):
    user_id = callback.from_user.id
    action = callback.data.removeprefix(CB_PREFIX)

    channel_handle = pending_channel_choice.pop(user_id, None)
    if not channel_handle:
        await callback.answer("Сессия истекла. Отправь канал заново.", show_alert=True)
        return

    username = callback.from_user.username or callback.from_user.first_name or ""

    if action == "custom":
        pending_custom_keywords[user_id] = channel_handle
        await callback.message.edit_text(
            f"✏️ Канал *@{channel_handle}* выбран.\n\n"
            "Напиши ключевые слова через запятую, например:\n"
            "`стипендия, scholarship, грант`",
            parse_mode="Markdown",
        )
        await callback.answer()
        return

    tpl = TEMPLATES.get(action)
    if not tpl:
        await callback.answer("Неизвестный шаблон.", show_alert=True)
        return

    _queue_direct_subscription(
        user_id,
        username,
        channel_handle,
        tpl["goal"],
        tpl["keywords"],
        audience_criteria=tpl.get("audience_criteria"),
    )
    await callback.message.edit_text(
        f"⏳ Настраиваю мониторинг *@{channel_handle}*…\n"
        f"Фильтр: {tpl['label']}",
        parse_mode="Markdown",
    )
    await callback.answer("Подписка оформляется…")
    print(f"[BOT] Direct subscribe {user_id} -> @{channel_handle} ({action})")


# ─── Free text (channel → inline templates, else LLM) ─────────────────────────

@dp.message(F.text)
async def handle_request(message: Message):
    user_id = message.from_user.id
    text = message.text.strip()
    tg_username = message.from_user.username or message.from_user.first_name or ""

    # Menu labels are handled by dedicated handlers above
    if text in (BTN_TRACK, BTN_LIST, BTN_HELP, BTN_CRITERIA):
        return

    # Audience criteria input
    if user_id in pending_criteria_input:
        pending_criteria_input.discard(user_id)
        if text.lower() in ("сброс", "reset", "очистить", "-"):
            await set_user_criteria(user_id, "")
            await message.answer(
                "✅ Критерии сброшены. Буду фильтровать только по категории канала.",
                reply_markup=main_menu_keyboard,
            )
            return
        await set_user_criteria(user_id, text)
        await message.answer(
            f"✅ Критерии сохранены:\n_{text}_\n\n"
            "Новые посты будут проверяться ИИ с учётом этого профиля.",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard,
        )
        return

    # Custom keywords flow (after «Свои ключевые слова»)
    if user_id in pending_custom_keywords:
        channel_handle = pending_custom_keywords.pop(user_id)
        keywords = [k.strip() for k in re.split(r"[,;]+", text) if k.strip()]
        if not keywords:
            pending_custom_keywords[user_id] = channel_handle
            await message.answer(
                "❌ Не удалось распознать ключевые слова.\n"
                "Отправь их через запятую, например: `python, django, backend`",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard,
            )
            return

        _queue_direct_subscription(
            user_id,
            tg_username,
            channel_handle,
            "свой фильтр",
            keywords,
        )
        await message.answer(
            f"⏳ Настраиваю мониторинг *@{channel_handle}*…\n"
            f"🔑 Ключевые слова: {', '.join(keywords)}",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard,
        )
        print(f"[BOT] Custom keywords subscribe {user_id} -> @{channel_handle}: {keywords}")
        return

    # @username or t.me/ link → inline category keyboard
    channel_handle = extract_channel_handle(text)
    if channel_handle:
        pending_channel_choice[user_id] = channel_handle
        pending_custom_keywords.pop(user_id, None)
        await message.answer(
            f"📡 Канал *@{channel_handle}*\n\n"
            "Что именно отслеживать в этом канале?",
            parse_mode="Markdown",
            reply_markup=template_keyboard(),
        )
        print(f"[BOT] Channel pick from {user_id}: @{channel_handle}")
        return

    # Fallback: natural-language request via LLM (legacy)
    pending_requests[user_id] = {
        "type": "llm",
        "text": text,
        "user_id": user_id,
        "username": tg_username,
    }
    await message.answer(
        "⏳ Обрабатываю запрос, настраиваю мониторинг…",
        reply_markup=main_menu_keyboard,
    )
    print(f"[BOT] LLM request from {user_id}: {text!r}")


# ─── Start ────────────────────────────────────────────────────────────────────

async def start_bot():
    print("[BOT] Starting…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(start_bot())
