"""Booker Lambda: invoked by EventBridge Scheduler ~3min before the window opens.

Flow: load booking -> per-user credentials -> login -> wait for the exact
window-open instant (500ms polling) -> book with retry -> notify the user,
and report every outcome (with the failure reason) to the admin chat.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Any

import booking_core as core
import booking_store
import telegram_api
import user_store
import gym_client

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


@dataclass
class Deps:
    telegram: Any = field(default_factory=lambda: telegram_api)
    gym: Any = field(default_factory=lambda: gym_client)
    users: Any = field(default_factory=lambda: user_store)
    bookings: Any = field(default_factory=lambda: booking_store)
    secrets: Any = field(default_factory=lambda: booking_store)  # get_user_credentials


def _notify(deps: Deps, chat_id: str, text: str) -> None:
    try:
        deps.telegram.send_message(chat_id, text)
    except Exception:
        logger.exception("Could not send Telegram notification")


def _report_admin(deps: Deps, user_chat_id: str, text: str) -> None:
    """Send the booking report to the admin chat — unless the booking user IS
    the admin (their personal message already carries the same information)."""
    admin_chat = os.environ.get("ADMIN_CHAT_ID", "").strip()
    if admin_chat and admin_chat != str(user_chat_id):
        _notify(deps, admin_chat, text)


def _fail(deps: Deps, user_id: str, booking_id: str, chat_id: str,
          label: str, reason: str) -> dict:
    deps.bookings.set_status(user_id, booking_id, "failed")
    _notify(deps, chat_id, f"❌ Booking failed: {label}\nReason: {reason}")
    _report_admin(deps, chat_id, f"📣 Booking report\n❌ FAILED — {label}\n"
                                 f"user: {user_id}\nreason: {reason}")
    return {"status": "failed", "reason": reason}


def process(event: dict, deps: Deps) -> dict:
    user_id = str(event.get("telegram_user_id", ""))
    booking_id = str(event.get("booking_id", ""))
    booking = deps.bookings.get_booking(user_id, booking_id)
    if not booking or booking.get("status") != "scheduled":
        logger.warning("Booking %s/%s missing or not scheduled — skipping", user_id, booking_id)
        return {"status": "skipped"}

    label = f"{booking['name']} on {booking['date']} {booking['time']}"
    chat_id = booking.get("chat_id", user_id)

    # Re-check access at booking time: a denied or removed user must never be booked,
    # even if a schedule slipped through the cancellation.
    owner = deps.users.get_user(user_id) or {}
    if owner.get("status") != "approved":
        logger.warning("User %s is no longer approved — not booking %s", user_id, booking_id)
        return _fail(deps, user_id, booking_id, chat_id, label, "user is no longer approved")

    try:
        creds = deps.secrets.get_user_credentials(user_id)

        token = deps.gym.login(creds["email"], creds["password"], creds["api_key"])
        if not token:
            return _fail(deps, user_id, booking_id, chat_id, label,
                         "Gym login failed (password changed? re-link with /login)")

        window_opens = core.window_opens_at(booking["date"], booking["time"],
                                            int(booking["window_minutes"]))
        core.wait_until_open(window_opens)

        last = {"outcome": "", "detail": ""}

        def once() -> str:
            outcome, detail = deps.gym.book_once(token, creds["api_key"], creds["club_id"],
                                                booking["class_id"], booking["session_id"],
                                                booking["date"])
            last.update(outcome=outcome, detail=detail)
            return outcome

        budget = int(os.environ.get("RETRY_WINDOW_SECONDS", "90"))
        success = core.book_with_retry(
            once, retry_window_seconds=budget,
            retry_interval_seconds=float(os.environ.get("RETRY_INTERVAL_SECONDS", "0.5")),
        )
    except Exception as e:
        logger.exception("Booker crashed for %s/%s", user_id, booking_id)
        deps.bookings.set_status(user_id, booking_id, "failed")
        _notify(deps, chat_id, f"❌ Booking failed: {label} — internal error.")
        _report_admin(deps, chat_id, f"📣 Booking report\n💥 CRASHED — {label}\n"
                                     f"user: {user_id}\nerror: {e}")
        return {"status": "crashed"}

    if success:
        deps.bookings.set_status(user_id, booking_id, "booked")
        _notify(deps, chat_id, f"✅ Booked: {label}")
        _report_admin(deps, chat_id, f"📣 Booking report\n✅ BOOKED — {label}\nuser: {user_id}")
        return {"status": "booked"}

    if last["outcome"] == "rejected":
        reason = f"rejected by the gym: {last['detail'] or 'no detail'}"
    else:
        reason = (f"gave up after {budget}s of retries "
                  f"(last outcome: {last['outcome'] or 'none'} {last['detail']})".strip())
    return _fail(deps, user_id, booking_id, chat_id, label, reason)


def lambda_handler(event: dict, context: Any) -> dict:
    return process(event, Deps())
