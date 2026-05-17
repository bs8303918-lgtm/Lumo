import qrcode
import httpx
import asyncio
import os
from typing import Optional
 
from config import GROQ_URL, GROQ_API_KEY, SYSTEM_PROMPT, HEAD_PROMPT, AUGMENTING_MODEL, MODEL
 
# ─── QR Auth ─────────────────────────────────────────────────────────────────
 
async def qr_auth(client) -> None:
    print("Генерирую QR-код для входа...")
    qr_login = await client.qr_login()
    qr = qrcode.make(qr_login.url)
    qr.save("login_qr.png")
    print("QR сохранен как login_qr.png. Отсканируй его в Настройки -> Устройства")
    await qr_login.wait()
    print("Вход выполнен успешно!")
 
# ─── Groq Service ─────────────────────────────────────────────────────────────
 
class GroqService:
    MAX_RETRIES = 3
    RETRY_DELAY = 5  # секунд между попытками при 429
 
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0)
        self.url = GROQ_URL
        self.headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }
 
    async def _post_with_retry(self, payload: dict) -> dict:
        """Выполняет POST запрос с повтором при 429 (rate limit)."""
        for attempt in range(1, self.MAX_RETRIES + 1):
            r = await self.client.post(self.url, headers=self.headers, json=payload)
 
            if r.status_code == 429:
                retry_after = int(r.headers.get("retry-after", self.RETRY_DELAY))
                print(f"[GROQ] Rate limit, жду {retry_after}s (попытка {attempt}/{self.MAX_RETRIES})")
                await asyncio.sleep(retry_after)
                continue
 
            r.raise_for_status()
            return r.json()
 
        raise Exception(f"[GROQ] Превышен лимит попыток ({self.MAX_RETRIES}) из-за rate limit")
 
    async def augment_prompt(self, prompt: str) -> str:
        full_prompt = HEAD_PROMPT + prompt
        payload = {
            "model": AUGMENTING_MODEL,
            "messages": [{"role": "user", "content": full_prompt}],
            "temperature": 0.1
        }
        data = await self._post_with_retry(payload)
        return data["choices"][0]["message"]["content"]
 
    async def ask(self, prompt: str) -> str:
        payload = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2
        }
        data = await self._post_with_retry(payload)
        return data["choices"][0]["message"]["content"]
 
    async def close(self):
        await self.client.aclose()
 