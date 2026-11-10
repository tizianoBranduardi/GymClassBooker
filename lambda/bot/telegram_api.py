"""Thin Telegram Bot API client. Token comes from Secrets Manager."""

import json
import logging
import os

import boto3
import requests

logger = logging.getLogger(__name__)
TIMEOUT = 10
_cache: dict[str, str] = {}


def _token() -> str:
    if "t" not in _cache:
        sm = boto3.client("secretsmanager")
        raw = sm.get_secret_value(SecretId=os.environ["TELEGRAM_SECRET_ID"])["SecretString"].strip()
        if raw.startswith("{"):
            data = json.loads(raw)
            raw = str(next(iter(data.values())))
        _cache["t"] = raw
    return _cache["t"]


def _call(method: str, payload: dict) -> dict:
    resp = requests.post(f"https://api.telegram.org/bot{_token()}/{method}", json=payload, timeout=TIMEOUT)
    if resp.status_code != 200:
        logger.error("Telegram %s returned HTTP %s", method, resp.status_code)
        resp.raise_for_status()
    return resp.json()


def send_message(chat_id: int | str, text: str, parse_mode: str | None = None,
                 reply_markup: dict | None = None) -> dict:
    payload: dict = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return _call("sendMessage", payload)


def answer_callback_query(callback_id: str, text: str = "") -> dict:
    """Acknowledge an inline-button press so Telegram stops the spinner."""
    return _call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def delete_message(chat_id: int | str, message_id: int) -> dict:
    return _call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
