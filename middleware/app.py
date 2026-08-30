"""
Middleware-сервіс перевірки підписки на канали.

Основний бот НЕ перевіряє підписку напряму через Telegram API.
Він надсилає запит сюди, а цей сервіс перевіряє — своїм окремим токеном.

Env vars:
  MIDDLEWARE_BOT_TOKEN  — токен окремого Telegram-бота (доданого до каналів)
  REQUIRED_CHANNELS     — ті ж канали що й у основного бота
                          формат: "id|Назва|url;id2|Назва2|url2"
  MIDDLEWARE_SECRET     — спільний секретний ключ з основним ботом
  PORT                  — порт (Railway задає автоматично)
"""

import os, logging, httpx
from flask import Flask, jsonify, request

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

BOT_TOKEN       = os.getenv("MIDDLEWARE_BOT_TOKEN", "")
MIDDLEWARE_SECRET = os.getenv("MIDDLEWARE_SECRET", "")

# Парсимо канали
_CHANNELS_RAW = os.getenv("REQUIRED_CHANNELS", "")
REQUIRED_CHANNELS: list[tuple[str, str, str]] = []
for _entry in _CHANNELS_RAW.split(";"):
    _parts = _entry.strip().split("|")
    if len(_parts) == 3:
        REQUIRED_CHANNELS.append((_parts[0].strip(), _parts[1].strip(), _parts[2].strip()))

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def _auth_ok() -> bool:
    """Перевіряє Bearer-токен у запиті."""
    if not MIDDLEWARE_SECRET:
        return True  # Секрет не задано — відкритий режим (не рекомендується)
    auth = request.headers.get("Authorization", "")
    return auth == f"Bearer {MIDDLEWARE_SECRET}"


def _get_chat_member(chat_id: str, user_id: int) -> str | None:
    """Повертає статус членства або None при помилці."""
    try:
        resp = httpx.get(
            f"{TG_API}/getChatMember",
            params={"chat_id": chat_id, "user_id": user_id},
            timeout=6,
        )
        data = resp.json()
        if data.get("ok"):
            return data["result"]["status"]
        log.warning("getChatMember error for %s: %s", chat_id, data.get("description"))
        return None
    except Exception as e:
        log.error("getChatMember exception for %s: %s", chat_id, e)
        return None


@app.get("/check")
def check_subscription():
    """
    GET /check?user_id=123456789
    Authorization: Bearer <MIDDLEWARE_SECRET>

    Response 200:
      {"ok": true}                          — підписаний на всі канали
      {"ok": false, "channels": [...]}      — не підписаний, список каналів
    Response 401: неправильний секрет
    Response 400: відсутній user_id
    """
    if not _auth_ok():
        return jsonify({"error": "Unauthorized"}), 401

    try:
        user_id = int(request.args.get("user_id", ""))
    except (ValueError, TypeError):
        return jsonify({"error": "user_id required"}), 400

    if not REQUIRED_CHANNELS:
        return jsonify({"ok": True, "channels": []})

    not_subbed = []
    for ch_id, ch_name, ch_url in REQUIRED_CHANNELS:
        status = _get_chat_member(ch_id, user_id)
        if status is None or status in ("left", "kicked", "banned"):
            not_subbed.append({"id": ch_id, "name": ch_name, "url": ch_url})

    if not_subbed:
        return jsonify({"ok": False, "channels": not_subbed})
    return jsonify({"ok": True, "channels": []})


@app.get("/health")
def health():
    return jsonify({"status": "ok", "channels": len(REQUIRED_CHANNELS)})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8081))
    log.info("Middleware starting on port %d, channels: %d", port, len(REQUIRED_CHANNELS))
    app.run(host="0.0.0.0", port=port, debug=False)
