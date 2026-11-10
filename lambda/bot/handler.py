"""Telegram webhook entry point: authentication and role-based dispatch.

Roles:
- Admin (ADMIN_USER_ID): approve/deny (text or buttons), /pending, /remove, /users,
  and every user command for their own account.
- Approved users: user commands (commands.py), guided /login, free text to the assistant.
- Pending users: enrollment rules (enrollment.py); /cancel withdraws the request.
- Denied users: a fixed refusal.

Security:
- The Function URL is public, but every request must carry the
  X-Telegram-Bot-Api-Secret-Token header matching the stored webhook secret.
- /login messages (which contain credentials) are deleted immediately.
"""

import hmac
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import boto3

import admin
import assistant
import booking_store
import commands
import enrollment
import messages
import scheduler_client
import telegram_api
import user_store
import gym_client

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_secret_cache: dict[str, str] = {}

ADMIN_HELP = commands.HELP_TEXT + (
    "\nAdmin:\n"
    "/pending — requests waiting for a decision\n"
    "/users — list everyone\n"
    "/approve <id> · /deny <id> · /remove <id>\n"
)
ADMIN_COMMANDS = ("/approve", "/deny", "/remove", "/pending", "/users")


@dataclass
class Deps:
    """Injectable collaborators (mocked in tests)."""
    telegram: Any = field(default_factory=lambda: telegram_api)
    gym: Any = field(default_factory=lambda: gym_client)
    users: Any = field(default_factory=lambda: user_store)
    bookings: Any = field(default_factory=lambda: booking_store)
    sched: Any = field(default_factory=lambda: scheduler_client)


def get_webhook_secret() -> str:
    if "v" not in _secret_cache:
        sm = boto3.client("secretsmanager")
        _secret_cache["v"] = sm.get_secret_value(
            SecretId=os.environ["WEBHOOK_SECRET_ID"]
        )["SecretString"].strip()
    return _secret_cache["v"]


def enqueue_assistant(chat_id: int, user_id: str, text: str) -> None:
    """Run the NL assistant in a separate async invocation of this same function,
    so the webhook can return 200 to Telegram immediately (no retry storms)."""
    boto3.client("lambda").invoke(
        FunctionName=os.environ["AWS_LAMBDA_FUNCTION_NAME"],
        InvocationType="Event",
        Payload=json.dumps({"assistant_job": {"chat_id": chat_id, "user_id": user_id,
                                              "text": text}}).encode(),
    )


def process_assistant_job(job: dict, deps: Deps) -> dict:
    user = deps.users.get_user(job["user_id"]) or {}
    assistant.handle(deps, job["chat_id"], job["user_id"], user, job["text"])
    return {"statusCode": 200, "body": "assistant done"}


def _identity(sender: dict) -> dict:
    """Name, username and language saved with a pending request."""
    return {
        "first_name": sender.get("first_name", ""),
        "last_name": sender.get("last_name", ""),
        "username": sender.get("username", ""),
        "language": messages.language_of(sender.get("language_code")),
    }


def _admin_dispatch(deps: Deps, chat_id: int, text: str) -> bool:
    """Handle admin-only commands. Returns True when the text was one of them."""
    if text.startswith("/approve"):
        admin.handle_text_decision(deps, chat_id, text, "approve")
    elif text.startswith("/deny"):
        admin.handle_text_decision(deps, chat_id, text, "deny")
    elif text.startswith("/remove"):
        admin.handle_remove(deps, chat_id, text)
    elif text.startswith("/pending"):
        admin.handle_pending(deps, chat_id)
    elif text.startswith("/users"):
        admin.handle_users(deps, chat_id)
    else:
        return False
    return True


def _linked_user(deps: Deps, chat_id: int, message_id: int, user_id: str, user: dict | None,
                 text: str, lang: str, help_text: str) -> None:
    """Commands and free text for a user who can use the bot."""
    if user and user.get("login_step"):
        if text.startswith("/stop"):
            commands.cancel_guided_login(deps, chat_id, user_id, lang)
            return
        if not text.startswith("/"):
            commands.continue_guided_login(deps, chat_id, message_id, user_id, user, text, lang)
            return
        deps.users.set_login_step(user_id, None)  # a command abandons the guided login

    if user and not text.startswith("/") and user.get("email"):
        enqueue_assistant(chat_id, user_id, text)
        return
    commands.dispatch(deps, chat_id, message_id, user_id, user, text, help_text, lang)


def _dispatch(deps: Deps, message: dict) -> None:
    sender = message.get("from", {})
    user_id = str(sender.get("id", ""))
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    text = (message.get("text") or "").strip()
    if not user_id or chat_id is None or not text:
        return
    lang = messages.language_of(sender.get("language_code"))

    if user_id == admin.admin_id():
        if _admin_dispatch(deps, chat_id, text):
            return
        _linked_user(deps, chat_id, message_id, user_id, deps.users.get_user(user_id),
                     text, lang, ADMIN_HELP)
        return

    user = deps.users.get_user(user_id)
    if user is None or user.get("status") == "pending":
        if user and text.startswith("/cancel"):
            enrollment.withdraw(deps, chat_id, user_id, lang)
            return
        enrollment.request_access(deps, chat_id, user_id, _identity(sender), user)
        return

    if user.get("status") != "approved":
        deps.telegram.send_message(chat_id, messages.t("not_authorized", lang))
        return

    _linked_user(deps, chat_id, message_id, user_id, user, text, lang, commands.HELP_TEXT)


def process_event(event: dict, deps: Deps) -> dict:
    """Verify the webhook secret, dispatch, always return 200 after auth."""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    supplied = headers.get("x-telegram-bot-api-secret-token", "")
    if not hmac.compare_digest(supplied.encode(), get_webhook_secret().encode()):
        logger.warning("Rejected request with missing/wrong webhook secret header")
        return {"statusCode": 403, "body": "forbidden"}

    try:
        update = json.loads(event.get("body") or "{}")
        if "callback_query" in update:
            admin.handle_callback(deps, update["callback_query"])
        elif update.get("message"):
            _dispatch(deps, update["message"])
    except Exception:
        # Never propagate: Telegram would retry and re-deliver (duplicate /login runs).
        logger.exception("Failed to process update")
    return {"statusCode": 200, "body": "ok"}


def lambda_handler(event: dict, context: Any) -> dict:
    if isinstance(event, dict) and "assistant_job" in event:  # internal async self-invoke
        return process_assistant_job(event["assistant_job"], Deps())
    return process_event(event, Deps())
