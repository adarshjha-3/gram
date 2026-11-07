"""Telegram Bot API integration with resilient long polling."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import requests


log = logging.getLogger(__name__)
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramBotManager:
    def __init__(self, token: str, on_message_callback: Callable[[dict], None]):
        self.token = (token or "").strip()
        self.on_message_callback = on_message_callback
        self.bot = None
        self._offset = 0
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "Telegram-Multi-Bot-Desk/1.0"})

    def _request(self, method: str, *, payload=None, timeout=35):
        if not self.token:
            raise ValueError("A Telegram bot token is required.")
        url = TELEGRAM_API.format(token=self.token, method=method)
        response = self._session.post(url, json=payload or {}, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            description = data.get("description", "Telegram API request failed")
            raise RuntimeError(description)
        return data.get("result")

    def verify(self) -> dict:
        """Validate token using getMe and return safe bot metadata."""
        result = self._request("getMe", timeout=15)
        self.bot = {
            "id": int(result["id"]),
            "name": result.get("first_name") or result.get("username") or "Telegram Bot",
            "username": result.get("username", ""),
            "is_bot": bool(result.get("is_bot", True)),
        }
        return dict(self.bot)

    def start_polling(self):
        """Start one daemon long-polling thread for this bot."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name=f"telegram-poll-{(self.bot or {}).get('id', 'unknown')}",
            daemon=True,
        )
        self._thread.start()

    def _poll_loop(self):
        backoff = 1
        while not self._stop_event.is_set():
            try:
                updates = self._request(
                    "getUpdates",
                    payload={
                        "offset": self._offset,
                        "timeout": 25,
                        "allowed_updates": ["message"],
                    },
                    timeout=35,
                )
                backoff = 1
                for update in updates or []:
                    self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                    message = update.get("message")
                    if not message or not message.get("chat"):
                        continue
                    try:
                        self.on_message_callback(message)
                    except Exception:
                        log.exception("Incoming message callback failed")
            except requests.RequestException as exc:
                log.warning("Telegram polling network error: %s", exc)
            except (RuntimeError, ValueError, KeyError) as exc:
                # Invalidated tokens / Telegram conflicts should not kill the process.
                log.warning("Telegram polling error: %s", exc)
            except Exception:
                log.exception("Unexpected polling error")

            if self._stop_event.wait(backoff):
                break
            backoff = min(backoff * 2, 15)

    def send_message(self, chat_id: int | str, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            raise ValueError("Message text cannot be empty.")
        if len(text) > 4096:
            raise ValueError("Telegram messages must be 4096 characters or fewer.")
        result = self._request(
            "sendMessage",
            payload={"chat_id": chat_id, "text": text},
            timeout=20,
        )
        return result

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._session.close()
