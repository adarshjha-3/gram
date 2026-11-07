"""Flask + Socket.IO backend for Telegram Multi-Bot Desk.

This app intentionally stores state in memory only. Restarting the process clears
bots, chats and message history. Protect the dashboard with ADMIN_PASSWORD.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit

from bot import TelegramBotManager


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.abspath(__file__))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me-now")

app = Flask(__name__, static_folder=None)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", secrets.token_hex(32))
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins=[])

# All application state is deliberately process-local and in-memory.
state_lock = threading.RLock()
bots: dict[str, dict] = {}
managers: dict[str, TelegramBotManager] = {}
token_to_bot: dict[str, str] = {}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def authorized():
    supplied = request.headers.get("X-Admin-Key", "")
    return bool(ADMIN_PASSWORD) and secrets.compare_digest(supplied, ADMIN_PASSWORD)


def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not authorized():
            return jsonify({"ok": False, "error": "Unauthorized. Enter the dashboard password."}), 401
        return fn(*args, **kwargs)
    return wrapper


def public_bot(bot):
    return {
        "id": bot["id"],
        "name": bot["name"],
        "username": bot["username"],
        "is_bot": bot["is_bot"],
        "created_at": bot["created_at"],
        "chat_count": len(bot["chats"]),
    }


def handle_incoming(bot_id: str, message: dict):
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = str(chat.get("id", ""))
    if not chat_id:
        return

    first = sender.get("first_name") or chat.get("first_name") or "Telegram User"
    last = sender.get("last_name") or chat.get("last_name") or ""
    username = sender.get("username") or chat.get("username") or ""
    display_name = (first + (" " + last if last else "")).strip()
    text = message.get("text") or message.get("caption") or "[Non-text message]"
    item = {
        "id": str(message.get("message_id", secrets.token_hex(6))),
        "bot_id": str(bot_id),
        "chat_id": chat_id,
        "direction": "incoming",
        "text": text,
        "timestamp": now_iso(),
        "from_name": display_name,
        "username": username,
    }

    with state_lock:
        bot = bots.get(str(bot_id))
        if not bot:
            return
        chat_record = bot["chats"].get(chat_id)
        if not chat_record:
            chat_record = {
                "chat_id": chat_id,
                "name": display_name,
                "username": username,
                "messages": [],
                "last_message": "",
                "last_timestamp": item["timestamp"],
            }
            bot["chats"][chat_id] = chat_record
        else:
            chat_record["name"] = display_name or chat_record["name"]
            chat_record["username"] = username or chat_record["username"]
        chat_record["messages"].append(item)
        chat_record["last_message"] = text
        chat_record["last_timestamp"] = item["timestamp"]

    socketio.emit("new_message", item)


@app.get("/")
def index():
    return send_from_directory(ROOT, "index.html")


@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    supplied = str(data.get("password", ""))
    if not secrets.compare_digest(supplied, ADMIN_PASSWORD):
        return jsonify({"ok": False, "error": "Incorrect dashboard password."}), 401
    return jsonify({"ok": True})


@app.post("/api/add_bot")
@require_admin
def add_bot():
    data = request.get_json(silent=True) or {}
    token = str(data.get("token", "")).strip()
    if not token:
        return jsonify({"ok": False, "error": "Bot token is required."}), 400

    with state_lock:
        existing_id = token_to_bot.get(token)
        if existing_id and existing_id in bots:
            return jsonify({"ok": False, "error": "This bot is already connected."}), 409

    manager = TelegramBotManager(token, lambda message: handle_incoming(str(manager.bot["id"]), message))
    try:
        metadata = manager.verify()
    except Exception as exc:
        logger.info("Bot verification failed: %s", exc)
        return jsonify({"ok": False, "error": f"Could not verify bot token: {exc}"}), 400

    bot_id = str(metadata["id"])
    with state_lock:
        if bot_id in bots:
            manager.stop()
            return jsonify({"ok": False, "error": "A bot with this Telegram ID is already connected."}), 409
        bots[bot_id] = {
            **metadata,
            "created_at": now_iso(),
            "chats": {},
        }
        managers[bot_id] = manager
        token_to_bot[token] = bot_id

    manager.start_polling()
    return jsonify({"ok": True, "bot": public_bot(bots[bot_id])}), 201


@app.get("/api/bots")
@require_admin
def get_bots():
    with state_lock:
        result = [public_bot(bot) for bot in bots.values()]
    return jsonify({"ok": True, "bots": result})


@app.get("/api/bot/<bot_id>/chats")
@require_admin
def get_chats(bot_id):
    with state_lock:
        bot = bots.get(str(bot_id))
        if not bot:
            return jsonify({"ok": False, "error": "Bot not found."}), 404
        chats = []
        for chat in bot["chats"].values():
            chats.append({
                "chat_id": chat["chat_id"],
                "name": chat["name"],
                "username": chat["username"],
                "last_message": chat["last_message"],
                "last_timestamp": chat["last_timestamp"],
                "message_count": len(chat["messages"]),
            })
    chats.sort(key=lambda item: item["last_timestamp"], reverse=True)
    return jsonify({"ok": True, "chats": chats})


@app.get("/api/bot/<bot_id>/chat/<chat_id>/messages")
@require_admin
def get_messages(bot_id, chat_id):
    with state_lock:
        bot = bots.get(str(bot_id))
        if not bot:
            return jsonify({"ok": False, "error": "Bot not found."}), 404
        chat = bot["chats"].get(str(chat_id))
        if not chat:
            return jsonify({"ok": True, "messages": []})
        messages = list(chat["messages"])
    return jsonify({"ok": True, "messages": messages})


@app.post("/api/send_message")
@require_admin
def send_message():
    data = request.get_json(silent=True) or {}
    bot_id = str(data.get("bot_id", "")).strip()
    chat_id = str(data.get("chat_id", "")).strip()
    text = str(data.get("text", "")).strip()
    if not bot_id or not chat_id or not text:
        return jsonify({"ok": False, "error": "bot_id, chat_id and text are required."}), 400

    with state_lock:
        manager = managers.get(bot_id)
        bot = bots.get(bot_id)
        chat = bot["chats"].get(chat_id) if bot else None
    if not manager or not bot or not chat:
        return jsonify({"ok": False, "error": "Bot or chat not found."}), 404

    try:
        result = manager.send_message(chat_id, text)
    except Exception as exc:
        logger.warning("sendMessage failed: %s", exc)
        return jsonify({"ok": False, "error": f"Telegram send failed: {exc}"}), 502

    outgoing = {
        "id": str(result.get("message_id", secrets.token_hex(6))),
        "bot_id": bot_id,
        "chat_id": chat_id,
        "direction": "outgoing",
        "text": result.get("text", text),
        "timestamp": now_iso(),
        "from_name": "You",
        "username": "",
    }
    with state_lock:
        chat["messages"].append(outgoing)
        chat["last_message"] = outgoing["text"]
        chat["last_timestamp"] = outgoing["timestamp"]

    socketio.emit("new_message", outgoing)
    return jsonify({"ok": True, "message": outgoing})


@socketio.on("connect")
def socket_connect(auth=None):
    supplied = (auth or {}).get("admin_key", "")
    if not secrets.compare_digest(str(supplied), ADMIN_PASSWORD):
        return False


@socketio.on("disconnect")
def socket_disconnect():
    logger.debug("Dashboard socket disconnected")


if __name__ == "__main__":
    # Use threading mode for predictable compatibility with current Python versions.
    socketio.run(
        app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "5000")),
        debug=False,
        allow_unsafe_werkzeug=True,
    )
