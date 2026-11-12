"""Optional push notifications (Telegram). Off unless ALERT_WEBHOOK=telegram.

Sending happens in a background thread so a slow/unreachable internet connection never
delays the reply to the Arduino.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import requests

log = logging.getLogger(__name__)


class TelegramAlerter:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.base = f"https://api.telegram.org/bot{bot_token}"
        self.chat_id = chat_id

    def __call__(self, message: str, image: Path | None = None) -> None:
        threading.Thread(target=self._send, args=(message, image), daemon=True).start()

    def _send(self, message: str, image: Path | None) -> None:
        try:
            if image and image.exists():
                with open(image, "rb") as f:
                    r = requests.post(f"{self.base}/sendPhoto", data={"chat_id": self.chat_id, "caption": message},
                                      files={"photo": f}, timeout=15)
            else:
                r = requests.post(f"{self.base}/sendMessage", data={"chat_id": self.chat_id, "text": message},
                                  timeout=15)
            if not r.ok:
                log.warning("Telegram alert failed: HTTP %s %s", r.status_code, r.text[:200])
        except requests.RequestException as exc:
            log.warning("Telegram alert failed: %s", exc)


def make_alerter(kind: str, bot_token: str, chat_id: str):
    if kind == "telegram":
        if not bot_token or not chat_id:
            log.warning("ALERT_WEBHOOK=telegram but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing -> alerts off")
            return None
        log.info("Telegram alerts enabled")
        return TelegramAlerter(bot_token, chat_id)
    if kind:
        log.warning("Unknown ALERT_WEBHOOK=%r (supported: telegram) -> alerts off", kind)
    return None
