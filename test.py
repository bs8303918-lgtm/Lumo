import sys

print("ARGs: ", end="")
print(*sys.argv)

msg = " ".join(sys.argv[1:])

import os
import asyncio
from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

api_id = os.getenv("CLIENT_API")
api_hash = os.getenv("CLIENT_API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

MY_CHAT_ID = 5559703828  # ← paste your chat_id here

async def main():
    bot = TelegramClient("bot", api_id, api_hash)
    await bot.start(bot_token=BOT_TOKEN)

    await bot.send_message(MY_CHAT_ID, msg)

    print("Message sent!")
    await bot.disconnect()

asyncio.run(main())