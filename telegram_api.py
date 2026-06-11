"""Minimal Telegram Bot API client (no heavy dependency)."""
import os
import httpx

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
API = f"https://api.telegram.org/bot{TOKEN}"


async def _call(method, **payload):
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(f"{API}/{method}", json=payload)
        return r.json()


async def send_message(chat_id, text, buttons=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    return await _call("sendMessage", **payload)


async def answer_callback(callback_id, text=None):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    return await _call("answerCallbackQuery", **payload)


async def set_webhook(url, secret):
    return await _call(
        "setWebhook",
        url=url,
        secret_token=secret,
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )


def button(text, data):
    return {"text": text, "callback_data": data}
