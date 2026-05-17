"""
processor.py — AI processing pipeline.

Responsibilities:
  1. Cheap keyword pre-filter  → skip obvious spam/ads without hitting Groq.
  2. Relevance check           → single Groq call, shared across all subscribers.
  3. Structured extraction     → one Groq call produces title/deadline/link/summary.
  4. Google Calendar link gen  → added when deadline is present.
  5. Rate-limited Groq queue   → asyncio.Semaphore prevents API hammering.
"""

import os
import re
import json
import html
import asyncio
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass

import httpx

from config import (
    GROQ_URL, GROQ_API_KEY,
    MODEL, AUGMENTING_MODEL,
    SYSTEM_PROMPT, HEAD_PROMPT,
)

# ─── Config ───────────────────────────────────────────────────────────────────

# Max concurrent requests to Groq (stay well under their RPM limit)
GROQ_CONCURRENCY = 5

# Spam signals: if post contains ANY of these patterns it's skipped before AI
SPAM_PATTERNS = [
    r"\b(реклама|sponsored|партнёрский|промокод|promo\s*code)\b",
    r"\b(казино|crypto\s*pump|100x|бесплатн[оы][её]\s*крипт)\b",
    r"(t\.me/joinchat/|t\.me/\+)",   # invite links typical for spam channels
    r"🎰|💸💸💸",
]
_SPAM_RE = re.compile("|".join(SPAM_PATTERNS), re.IGNORECASE)

# ─── Structured output schema expected from LLM ──────────────────────────────

EXTRACT_PROMPT = """You are an assistant that extracts opportunity details from a Telegram post.

Return ONLY valid JSON with this exact structure:
{
  "title": "short title of the opportunity",
  "deadline": "YYYY-MM-DD or null if not found",
  "link": "URL from post or null",
  "summary": "2-3 sentence neutral summary",
  "audience_fit": "who this is for, e.g. школьники / студенты / все"
}

Post:
"""

RELEVANCE_PROMPT_TPL = """You are a relevance filter for a Telegram monitoring bot.

Goal: {goal}
Keywords: {keywords}
User audience criteria: {audience}

Rules:
- YES if the post matches the goal/keywords AND fits the user audience (if criteria is set).
- If audience criteria says "school student / школьник", approve hackathons/competitions open to school students; say NO if the event is clearly ONLY for adults, university-only, or professionals with no school track.
- Short posts with a link preview count — use title/description from the post text.
- YES for obvious hackathon/grant posts when keywords match, even if the caption is short.

Post:
{text}

Is this post relevant? Reply ONLY 'YES: <one-line reason>' or 'NO'."""

# Predefined audience profiles (also stored on user when template is picked)
AUDIENCE_SCHOOL = (
    "Школьник: нужны олимпиады, конкурсы, хакатоны и гранты именно для школьников "
    "(9–11 класс, K-12). Не присылать события только для студентов вузов или профессионалов, "
    "если в посте явно нет школьной категории."
)

OPPORTUNITY_KEYWORDS = re.compile(
    r"хакатон|hackathon|грант|grant|конкурс|стипенд|olympiad|олимпиад|"
    r"ваканс|internship|стажиров|synergy|nis\b|дедлайн|deadline",
    re.IGNORECASE,
)

# RU/EN variants for user-defined keywords
KEYWORD_VARIANTS: dict[str, list[str]] = {
    "hackathon": ["hackathons", "хакатон", "хакатоны", "хакатона"],
    "hackathons": ["hackathon", "хакатон", "хакатоны"],
    "хакатон": ["hackathon", "hackathons", "хакатоны"],
    "astana": ["астана", "астане"],
    "астана": ["astana"],
    "competition": ["competitions", "конкурс", "конкурсы"],
    "competitions": ["competition", "конкурс", "конкурсы"],
    "конкурс": ["competition", "competitions", "конкурсы"],
    "grant": ["grants", "грант", "гранты"],
    "грант": ["grant", "grants"],
    "school": ["школ", "школьник", "nis", "ученик", "k-12"],
    "школьник": ["school", "nis", "ученик", "школ"],
    "internship": ["стажировка", "стажировки"],
    "стажировка": ["internship", "intern"],
    "job": ["вакансия", "работа", "jobs"],
    "вакансия": ["job", "jobs", "vacancy"],
}

RU_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5, "май": 5,
    "июн": 6, "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}


class GroqAPIError(Exception):
    """Groq HTTP/API failure."""
    def __init__(self, status_code: int, message: str = ""):
        self.status_code = status_code
        super().__init__(message or f"Groq API error {status_code}")


# ─── Rate-limited Groq client ─────────────────────────────────────────────────

class GroqService:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=30.0)
        self._sem = asyncio.Semaphore(GROQ_CONCURRENCY)

    def _headers(self) -> dict:
        key = (os.getenv("GROQ_API_KEY") or GROQ_API_KEY or "").strip().strip('"').strip("'")
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def api_key_fingerprint() -> str:
        key = (os.getenv("GROQ_API_KEY") or GROQ_API_KEY or "").strip()
        if not key:
            return "(not set)"
        return f"{key[:7]}…{key[-4:]}" if len(key) > 12 else "(too short)"

    async def _call(self, model: str, messages: list, temperature: float = 0.2) -> str:
        payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": 512}
        async with self._sem:
            for attempt in range(1, 4):
                r = await self._client.post(GROQ_URL, headers=self._headers(), json=payload)
                if r.status_code == 429:
                    wait = int(r.headers.get("retry-after", 10))
                    print(f"[GROQ] Rate limit – waiting {wait}s (attempt {attempt}/3)")
                    await asyncio.sleep(wait)
                    continue
                if r.status_code == 401:
                    raise GroqAPIError(401, "Invalid or expired GROQ_API_KEY")
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"]
        raise RuntimeError("[GROQ] Max retries exceeded due to rate limit")

    async def augment(self, user_request: str) -> str:
        return await self._call(
            AUGMENTING_MODEL,
            [{"role": "user", "content": HEAD_PROMPT + user_request}],
            temperature=0.1,
        )

    async def classify_intent(self, augmented: str) -> str:
        return await self._call(
            MODEL,
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": augmented},
            ],
        )

    async def check_relevance(
        self,
        text: str,
        goal: str,
        keywords: list,
        audience_criteria: str = "",
    ) -> tuple[bool, str]:
        """Returns (is_relevant, reason)."""
        prompt = RELEVANCE_PROMPT_TPL.format(
            goal=goal,
            keywords=", ".join(keywords) if keywords else "any",
            audience=audience_criteria or "не заданы (подходит всем)",
            text=text[:2000],
        )
        verdict = await self._call(MODEL, [{"role": "user", "content": prompt}])
        verdict = verdict.strip()
        if verdict.upper().startswith("YES"):
            return True, verdict[4:].strip()
        return False, ""

    async def extract_structured(self, text: str) -> Optional[dict]:
        """Returns parsed dict or None on failure."""
        raw = await self._call(
            MODEL,
            [{"role": "user", "content": EXTRACT_PROMPT + text[:2000]}],
            temperature=0.1,
        )
        try:
            clean = re.sub(r"```(?:json)?|```", "", raw).strip()
            return json.loads(clean)
        except json.JSONDecodeError:
            print(f"[PROCESSOR] JSON parse failed: {raw[:100]}")
            return None

    async def close(self) -> None:
        await self._client.aclose()


# ─── Pre-filter (free, no API call) ──────────────────────────────────────────

def is_spam(text: str) -> bool:
    """True if the post looks like spam/ad and should be skipped."""
    return bool(_SPAM_RE.search(text))


def keyword_match(text: str, keywords: list) -> bool:
    """
    True if text contains at least one keyword (case-insensitive, RU/EN variants).
    If keyword list is empty, always returns True (track everything).
    """
    if not keywords:
        return True
    lower = text.lower()
    for kw in keywords:
        base = kw.lower().strip()
        variants = {base}
        variants.update(KEYWORD_VARIANTS.get(base, []))
        if any(v in lower for v in variants):
            return True
    # Global opportunity signals count as match for hackathon-style goals
    if OPPORTUNITY_KEYWORDS.search(lower):
        return True
    return False


def passes_audience_heuristic(text: str, audience_criteria: str) -> bool:
    """Rough audience filter when Groq is unavailable."""
    if not audience_criteria:
        return True
    lower = text.lower()
    crit = audience_criteria.lower()
    if "школ" in crit or "school" in crit:
        school_signals = ["nis", "школ", "ученик", "олимпиад", "k-12", "класс", "школьник"]
        if any(s in lower for s in school_signals):
            return True
        # Open hackathon without age limit — allow with note
        if "хакатон" in lower or "hackathon" in lower:
            if re.search(r"только\s+(студент|вуз|university|18\+|professionals)", lower):
                return False
            return True
        return False
    return True


def heuristic_extract(text: str) -> dict:
    """Parse title/deadline/link without LLM from typical TG post format."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    title = lines[0][:120] if lines else "Событие из канала"

    for pat in (
        r"хакатон[^\n]{0,80}",
        r"hackathon[^\n]{0,80}",
        r"конкурс[^\n]{0,80}",
    ):
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            title = m.group(0).strip()
            break

    deadline = None
    dm = re.search(
        r"дедлайн[:\s]*(\d{1,2})\s+([а-яё]+)\s+(\d{4})",
        text,
        re.IGNORECASE,
    )
    if dm:
        day, month_word, year = dm.groups()
        month = next((n for k, n in RU_MONTHS.items() if k in month_word.lower()), None)
        if month:
            deadline = f"{year}-{month:02d}-{int(day):02d}"

    if not deadline:
        dm2 = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
        if dm2:
            deadline = dm2.group(0)

    link = None
    for url in re.findall(r"https?://\S+", text):
        if "t.me/" not in url or "/+" not in url:
            link = url.rstrip(".,)")
            break

    summary = text[:350].replace("\n", " ")
    audience = "школьники" if re.search(r"\bnis\b|школ|ученик", text, re.I) else ""

    return {
        "title": title,
        "deadline": deadline,
        "link": link,
        "summary": summary,
        "audience_fit": audience,
    }


def extract_post_text(event) -> str:
    """
    Full text for AI: caption + link preview title/description + URLs.
    Fixes missed posts like «Есть хакатон» + hackathon.alem.ai preview.
    """
    msg = getattr(event, "message", None)
    parts: list[str] = []

    body = (getattr(event, "raw_text", None) or "").strip()
    if msg:
        body = (msg.message or msg.text or body or "").strip()

    if body:
        parts.append(body)

    if msg and msg.media:
        try:
            from telethon.tl.types import MessageMediaWebPage

            if isinstance(msg.media, MessageMediaWebPage) and msg.media.webpage:
                wp = msg.media.webpage
                for attr in ("title", "description", "site_name"):
                    val = getattr(wp, attr, None)
                    if val and str(val) not in body:
                        parts.append(str(val))
                url = getattr(wp, "url", None)
                if url:
                    parts.append(str(url))
        except Exception:
            pass

    for url in re.findall(r"https?://\S+", body):
        if url not in "\n".join(parts):
            parts.append(url)

    return "\n".join(parts).strip()


def should_process_post(text: str, keywords: list | None = None) -> bool:
    """Allow short posts when keywords/URLs/opportunity signals are present."""
    if not text or not text.strip():
        return False
    t = text.strip()
    if len(t) >= 15:
        return True
    if keywords and keyword_match(t, keywords):
        return True
    if re.search(r"https?://", t):
        return True
    if OPPORTUNITY_KEYWORDS.search(t):
        return True
    return len(t) >= 8


# ─── Google Calendar link ────────────────────────────────────────────────────

def make_gcal_link(title: str, deadline_str: Optional[str], description: str = "") -> Optional[str]:
    """
    Returns a Google Calendar 'add event' URL or None if deadline is absent/unparseable.
    The event is set as a 1-hour slot starting at 09:00 on the deadline date.
    """
    if not deadline_str:
        return None
    try:
        dt = datetime.strptime(deadline_str, "%Y-%m-%d")
    except ValueError:
        return None

    start = dt.replace(hour=9, minute=0, second=0)
    end = start + timedelta(hours=1)

    fmt = "%Y%m%dT%H%M%SZ"
    params = {
        "action": "TEMPLATE",
        "text": title,
        "dates": f"{start.strftime(fmt)}/{end.strftime(fmt)}",
        "details": description[:500],
    }
    return "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode(params)


# ─── Formatted bot message ───────────────────────────────────────────────────

def _esc(text: str) -> str:
    """Escape dynamic text for Telegram HTML parse_mode."""
    return html.escape(str(text), quote=False)


def _esc_url(url: str) -> str:
    return html.escape(str(url), quote=True)


def format_opportunity_plain(
    structured: dict, channel_username: str, reason: str
) -> str:
    """Plain-text notification (fallback when HTML fails)."""
    title = structured.get("title") or "Без названия"
    summary = (structured.get("summary") or "—")[:500]
    link = structured.get("link")
    deadline = structured.get("deadline") or "не указан"
    lines = [
        "🚀 НАЙДЕНО НОВОЕ СОБЫТИЕ!",
        "",
        f"📍 Источник: @{channel_username}",
        "",
        f"📝 Что: {title}",
        f"⏳ Дедлайн: {deadline}",
        "",
        f"💡 Описание: {summary}",
    ]
    if link:
        lines.append(f"\n🔗 {link}")
    audience = structured.get("audience_fit")
    if audience:
        lines.append(f"\n🎯 Для кого: {audience}")
    if reason:
        lines.append(f"\n✅ {reason}")
    return "\n".join(lines)


def format_opportunity(structured: dict, channel_username: str, reason: str) -> tuple[str, Optional[str]]:
    """
    Returns (message_text, gcal_link_or_None).
    Uses HTML parse_mode — safe with underscores in URLs and post text.
    """
    title = structured.get("title") or "Без названия"
    summary = structured.get("summary") or "—"
    link = structured.get("link")
    deadline = structured.get("deadline") or "не указан"

    gcal = make_gcal_link(title, structured.get("deadline"), summary)

    lines = [
        "🚀 <b>НАЙДЕНО НОВОЕ СОБЫТИЕ!</b>",
        "",
        f"📍 <b>Источник:</b> @{_esc(channel_username)}",
        "",
        f"📝 <b>Что:</b> {_esc(title)}",
        f"⏳ <b>Дедлайн:</b> <code>{_esc(deadline)}</code>",
        "",
        f"💡 <b>Описание:</b> {_esc(summary[:800])}",
    ]
    if link:
        lines.append(f'\n🔗 <a href="{_esc_url(link)}">Открыть регистрацию</a>')
    audience = structured.get("audience_fit")
    if audience:
        lines.append(f"\n🎯 <b>Для кого:</b> {_esc(audience)}")
    if reason:
        lines.append(f"\n✅ <i>{_esc(reason)}</i>")
    return "\n".join(lines), gcal


# ─── Core post processor ──────────────────────────────────────────────────────

@dataclass
class PostResult:
    """Outcome of processing a single post against one subscriber group."""
    is_relevant: bool
    reason: str
    structured: Optional[dict]
    message: Optional[str]
    gcal_link: Optional[str]


async def process_post(
    groq: GroqService,
    text: str,
    channel_username: str,
    goal: str,
    keywords: list,
    audience_criteria: str = "",
) -> PostResult:
    """
    Full pipeline for one (post, subscriber-group) pair.
    Called once per unique (goal, keywords) combination per post — not once per user.
    """
    # 1. Spam pre-filter
    if is_spam(text):
        return PostResult(False, "spam", None, None, None)

    # 2. Keyword pre-filter
    if not keyword_match(text, keywords):
        print(f"[PROCESSOR] No keyword match for @{channel_username}")
        return PostResult(False, "no keyword match", None, None, None)

    relevant = False
    reason = ""
    ai_ok = True

    # 3. AI relevance check (+ audience criteria)
    try:
        relevant, reason = await groq.check_relevance(
            text, goal, keywords, audience_criteria=audience_criteria
        )
    except GroqAPIError as e:
        ai_ok = False
        print(f"[PROCESSOR] Groq auth/error {e.status_code} — using heuristic fallback")
        if passes_audience_heuristic(text, audience_criteria):
            relevant = True
            reason = "найдено по ключевым словам"
        else:
            return PostResult(False, "audience mismatch (heuristic)", None, None, None)
    except Exception as e:
        ai_ok = False
        print(f"[PROCESSOR] Groq failed: {e}")
        if passes_audience_heuristic(text, audience_criteria):
            relevant = True
            reason = "найдено по ключевым словам"
        else:
            return PostResult(False, "ai error", None, None, None)

    if not relevant:
        return PostResult(False, "not relevant", None, None, None)

    # 4. Structured extraction
    structured = None
    if ai_ok:
        try:
            structured = await groq.extract_structured(text)
        except Exception as e:
            print(f"[PROCESSOR] Extract failed: {e}")

    if not structured:
        structured = heuristic_extract(text)
        if not ai_ok and "без ИИ" not in reason:
            reason = reason + " · карточка без ИИ"

    message, gcal = format_opportunity(structured, channel_username, reason)
    return PostResult(True, reason, structured, message, gcal)