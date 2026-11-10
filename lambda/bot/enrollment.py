"""Enrollment rules for users who are not approved yet.

- First contact creates a pending request and notifies the admin once.
- A pending user who writes again gets a reminder to the admin at most once
  per COOLDOWN, never a flood.
- A request older than EXPIRY is treated as new (the admin missed it for good).
- At most MAX_PENDING requests can be waiting, so the admin's inbox stays sane.
- /cancel withdraws a pending request.
"""

from datetime import datetime, timedelta, timezone

import admin
import messages

COOLDOWN = timedelta(hours=1)
EXPIRY = timedelta(days=7)
MAX_PENDING = 25


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _requested_at(user: dict) -> datetime:
    return datetime.fromisoformat(user["requested_at"])


def _is_expired(user: dict, now: datetime) -> bool:
    return now - _requested_at(user) >= EXPIRY


def request_access(deps, chat_id: int, user_id: str, identity: dict, user: dict | None) -> None:
    """Handle a message from someone who is not approved (new or pending)."""
    lang = identity.get("language", "en")
    now = now_utc()

    if user is None or _is_expired(user, now):
        if user is None and deps.users.count_pending() >= MAX_PENDING:
            deps.telegram.send_message(chat_id, messages.t("too_many", lang))
            return
        deps.users.create_pending(telegram_user_id=user_id, chat_id=str(chat_id), **identity)
        deps.telegram.send_message(chat_id, messages.t("requested", lang, name=identity.get("first_name", "")))
        admin.notify_request(deps, user_id, identity)
        return

    if now - _requested_at(user) >= COOLDOWN:
        deps.users.touch_request(user_id)
        deps.telegram.send_message(chat_id, messages.t("reminded", lang))
        admin.notify_request(deps, user_id, identity, reminder=True)
        return

    deps.telegram.send_message(chat_id, messages.t("already_pending", lang))


def withdraw(deps, chat_id: int, user_id: str, lang: str) -> None:
    deps.users.delete_user(user_id)
    deps.telegram.send_message(chat_id, messages.t("withdrawn", lang))
