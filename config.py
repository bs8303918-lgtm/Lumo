import os
from pathlib import Path

from dotenv import load_dotenv

# Always load .env from the project folder (not from random cwd)
_ROOT = Path(__file__).resolve().parent
load_dotenv(_ROOT / ".env", override=True)


def _env(name: str, default: str | None = None) -> str | None:
    val = os.getenv(name, default)
    if val is None:
        return None
    return val.strip().strip('"').strip("'")


# ─── Models ───────────────────────────────────────────────────────────────────
MODEL = "llama-3.3-70b-versatile"
AUGMENTING_MODEL = "llama-3.1-8b-instant"

# ─── Groq ─────────────────────────────────────────────────────────────────────
GROQ_API_KEY = _env("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# ─── System Prompt ────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """
You are a Telegram channel tracking assistant.

Read the user's message and extract their intent.

Rules:
- Only extract channels that are explicitly mentioned (@username or t.me/link).
- Do NOT invent or guess channel usernames.
- If no channel is mentioned, return an empty channels list.
- If the user is just asking a question (not requesting monitoring), return an empty channels list.
- Extract keywords/topics the user wants to track. If none, return [].
- goal must be one of: "monitor", "digest", "alerts", "summary"
- cadence must be one of: "immediate", "daily", "weekly"
- If channel identity is truly unclear, set ambiguous=true and write a clarification question.

You MUST respond with ONLY valid JSON — no markdown, no explanation, no extra text:
{
  "channels": ["@username1", "@username2"],
  "goal": "monitor",
  "keywords": ["keyword1", "keyword2"],
  "cadence": "immediate",
  "ambiguous": false,
  "clarification_needed": ""
}
"""

HEAD_PROMPT = """Rewrite the following user request into a clear, structured instruction for a Telegram channel tracking assistant.
Do NOT invent channel names. Only clarify and normalize what is already there.
If cadence is not stated, use "immediate". If goal is not stated, use "monitor".

User request:
"""
