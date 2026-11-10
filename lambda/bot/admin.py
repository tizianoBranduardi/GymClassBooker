"""Admin-only actions: approve, deny, remove, pending list, and the request notice.

Every decision is logged as an AUDIT line (who, what, which user) in CloudWatch.
"""

import logging
import os

import messages

logger = logging.getLogger(__name__)

APPROVE, DENY = "approve", "deny"


def admin_id() -> str:
    return os.environ["ADMIN_USER_ID"].strip()


def _display(identity_or_row: dict) -> str:
    name = " ".join(p for p in (identity_or_row.get("first_name", ""), identity_or_row.get("last_name", "")) if p)
    return name or "(no name)"


def _handle_text(identity_or_row: dict) -> str:
    username = identity_or_row.get("username")
    return f"@{username}" if username else "no username"


def _buttons(user_id: str) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": f"{APPROVE}:{user_id}"},
        {"text": "🚫 Deny", "callback_data": f"{DENY}:{user_id}"},
    ]]}


def notify_request(deps, user_id: str, identity: dict, reminder: bool = False) -> None:
    """Message the admin only: who is asking, with one-tap approve/deny."""
    header = "🔁 Reminder — still waiting" if reminder else "🔔 Access request"
    text = (
        f"{header}\n"
        f"👤 {_display(identity)} ({_handle_text(identity)})\n"
        f"🆔 {user_id}\n"
        f"🌐 {identity.get('language', 'en')}"
    )
    deps.telegram.send_message(admin_id(), text, reply_markup=_buttons(user_id))


def _audit(action: str, actor: str, target: str) -> None:
    logger.info("AUDIT action=%s actor=%s target=%s", action, actor, target)


def _cancel_schedules(deps, user_id: str) -> int:
    cancelled = 0
    for booking in deps.bookings.list_bookings(user_id):
        if booking.get("status") != "scheduled":
            continue
        if booking.get("schedule_name"):
            deps.sched.delete_schedule(booking["schedule_name"])
        deps.bookings.set_status(user_id, booking["booking_id"], "cancelled")
        cancelled += 1
    return cancelled


def _decide(deps, chat_id: int, target_id: str, decision: str, actor: str) -> None:
    target = deps.users.get_user(target_id)
    if not target:
        deps.telegram.send_message(chat_id, f"No request found for id {target_id}.")
        return
    lang = target.get("language", "en")
    name = _display(target)

    if decision == APPROVE:
        if target.get("status") == "approved":
            deps.telegram.send_message(chat_id, f"{name} is already approved.")
            return
        deps.users.set_status(target_id, "approved")
        _audit("approve", actor, target_id)
        deps.telegram.send_message(target["chat_id"], messages.t("approved", lang))
        deps.telegram.send_message(chat_id, f"✅ Approved: {name} (id {target_id}).")
        return

    if target.get("status") == "denied":
        deps.telegram.send_message(chat_id, f"{name} is already denied.")
        return
    cancelled = _cancel_schedules(deps, target_id)
    deps.users.revoke_access(target_id)
    _audit("deny", actor, target_id)
    deps.telegram.send_message(target["chat_id"], messages.t("denied", lang))
    deps.telegram.send_message(
        chat_id, f"🚫 Denied: {name} (id {target_id}). Credentials deleted, {cancelled} schedule(s) cancelled."
    )


def _parse_id(text: str) -> str | None:
    parts = text.split()
    return parts[1] if len(parts) == 2 and parts[1].isdigit() else None


def handle_text_decision(deps, chat_id: int, text: str, decision: str) -> None:
    target_id = _parse_id(text)
    if target_id is None:
        deps.telegram.send_message(chat_id, f"Usage: /{decision} <telegram user id>")
        return
    _decide(deps, chat_id, target_id, decision, actor=admin_id())


def handle_callback(deps, callback: dict) -> None:
    """Inline-button press. Only the admin's own presses are honoured."""
    callback_id = callback.get("id", "")
    actor = str(callback.get("from", {}).get("id", ""))
    if actor != admin_id():
        deps.telegram.answer_callback_query(callback_id, "Not allowed.")
        return
    action, _, target_id = (callback.get("data") or "").partition(":")
    if action not in (APPROVE, DENY) or not target_id.isdigit():
        deps.telegram.answer_callback_query(callback_id, "Unknown action.")
        return
    chat_id = callback.get("message", {}).get("chat", {}).get("id", actor)
    _decide(deps, chat_id, target_id, action, actor=actor)
    deps.telegram.answer_callback_query(callback_id, "Done ✔")


def handle_remove(deps, chat_id: int, text: str) -> None:
    target_id = _parse_id(text)
    if target_id is None:
        deps.telegram.send_message(chat_id, "Usage: /remove <telegram user id>")
        return
    target = deps.users.get_user(target_id)
    if not target:
        deps.telegram.send_message(chat_id, f"No user with id {target_id}.")
        return
    cancelled = _cancel_schedules(deps, target_id)
    deps.users.delete_user(target_id)
    _audit("remove", admin_id(), target_id)
    deps.telegram.send_message(
        chat_id, f"🗑 Removed {_display(target)} (id {target_id}). {cancelled} schedule(s) cancelled."
    )


def handle_pending(deps, chat_id: int) -> None:
    pending = [u for u in deps.users.list_users() if u.get("status") == "pending"]
    if not pending:
        deps.telegram.send_message(chat_id, "No pending requests. 🎉")
        return
    for user in pending[:10]:
        deps.telegram.send_message(
            chat_id,
            f"⏳ {_display(user)} ({_handle_text(user)}) — id {user['telegram_user_id']}",
            reply_markup=_buttons(user["telegram_user_id"]),
        )


def handle_users(deps, chat_id: int) -> None:
    users = deps.users.list_users()
    if not users:
        deps.telegram.send_message(chat_id, "No users yet.")
        return
    icons = {"approved": "✅", "pending": "⏳", "denied": "🚫"}
    lines = [
        f"{icons.get(u.get('status'), '•')} {_display(u)} ({_handle_text(u)}) — id {u['telegram_user_id']} — {u.get('status', '?')}"
        + (f" — {u['email']}" if u.get("email") else "")
        for u in users
    ]
    deps.telegram.send_message(chat_id, "Users:\n" + "\n".join(lines))
