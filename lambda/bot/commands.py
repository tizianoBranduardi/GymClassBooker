"""User-facing commands: /login, /status, /search, /pick, /bookings, /cancel."""

import logging
import os
from datetime import datetime

import booking_core
import booking_store
import fmt
import messages

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "Commands:\n"
    "/login — link your gym account (guided: email, then password)\n"
    "/status — show your linked account\n"
    "/search <YYYY-MM-DD> <HH:MM> — classes in a 3h window, that day + next 3\n"
    "/pick <n> — schedule the booking for result n\n"
    "/bookings — list scheduled bookings\n"
    "/cancel <booking id> — cancel a scheduled booking\n"
)

REGULAR_WINDOW_MINUTES = 2880
PRIORITY_WINDOW_MINUTES = 8640


def _is_linked(user: dict | None) -> bool:
    return bool(user and user.get("email"))


def _link(deps, chat_id: int, user_id: str, email: str, password: str, lang: str) -> None:
    result = deps.gym.validate(email, password)
    if not result.get("ok"):
        deps.telegram.send_message(chat_id, messages.t("login_failed", lang))
        return
    club_id = os.environ.get("DEFAULT_CLUB_ID", "207")
    deps.users.link_credentials(
        telegram_user_id=user_id, chat_id=str(chat_id), email=email,
        password=password, club_id=club_id, priority=bool(result.get("priority")),
    )
    member = "PRIORITY" if result.get("priority") else "REGULAR"
    deps.telegram.send_message(
        chat_id, messages.t("login_linked", lang, email=email, member=member, club=club_id)
    )


def handle_login(deps, chat_id: int, message_id: int, user_id: str, text: str, lang: str = "en") -> None:
    """/login alone starts the guided flow; /login <email> <password> links in one go."""
    try:
        deps.telegram.delete_message(chat_id, message_id)
    except Exception:
        logger.warning("Could not delete /login message %s in chat %s", message_id, chat_id)

    parts = text.split()
    if len(parts) == 1:
        deps.users.set_login_step(user_id, "email")
        deps.telegram.send_message(chat_id, messages.t("login_ask_email", lang))
        return
    if len(parts) != 3:
        deps.telegram.send_message(chat_id, "Usage: /login  (guided)  or  /login <email> <password>")
        return
    _link(deps, chat_id, user_id, parts[1], parts[2], lang)


def continue_guided_login(deps, chat_id: int, message_id: int, user_id: str, user: dict, text: str,
                          lang: str = "en") -> None:
    """Next step of the guided /login: first the email, then the password."""
    step = user.get("login_step")
    if step == "email":
        email = text.strip()
        if "@" not in email or " " in email:
            deps.telegram.send_message(chat_id, messages.t("login_bad_email", lang))
            return
        deps.users.set_login_step(user_id, "password", email=email)
        deps.telegram.send_message(chat_id, messages.t("login_ask_password", lang))
        return

    # step == "password": delete the message before anything else
    try:
        deps.telegram.delete_message(chat_id, message_id)
    except Exception:
        logger.warning("Could not delete password message %s in chat %s", message_id, chat_id)
    deps.users.set_login_step(user_id, None)
    _link(deps, chat_id, user_id, user.get("pending_email", ""), text, lang)


def cancel_guided_login(deps, chat_id: int, user_id: str, lang: str = "en") -> None:
    deps.users.set_login_step(user_id, None)
    deps.telegram.send_message(chat_id, messages.t("login_aborted", lang))


def handle_status(deps, chat_id: int, user: dict | None) -> None:
    if not _is_linked(user):
        deps.telegram.send_message(chat_id, "No gym account linked yet. Use /login <email> <password>.")
        return
    deps.telegram.send_message(chat_id, fmt.format_status(user), parse_mode="HTML")


def handle_search(deps, chat_id: int, user_id: str, user: dict, text: str) -> None:
    parts = text.split()
    usage = "Usage: /search <YYYY-MM-DD> <HH:MM>  (e.g. /search 2026-10-30 18:00)"
    if len(parts) != 3:
        deps.telegram.send_message(chat_id, usage)
        return
    try:
        datetime.strptime(f"{parts[1]} {parts[2]}", "%Y-%m-%d %H:%M")
    except ValueError:
        deps.telegram.send_message(chat_id, usage)
        return

    options = deps.gym.search_options(
        telegram_user_id=user_id, club_id=user.get("club_id"),
        target_date=parts[1], target_time=parts[2],
    )
    if options is None:
        deps.telegram.send_message(chat_id, "❌ Gym login failed — re-link with /login if your password changed.")
        return
    if not options:
        deps.telegram.send_message(chat_id, "🤷 No classes found in that window.")
        return

    deps.users.save_search(user_id, options)
    deps.telegram.send_message(chat_id, fmt.format_options(options), parse_mode="HTML")


def pick_option(deps, chat_id: int, user_id: str, user: dict, n: int) -> tuple[str, dict | str]:
    """Schedule option n from the latest search. Shared by /pick and the assistant.

    Returns ("scheduled"|"immediate", payload) on success,
    ("no_search"|"invalid", info) otherwise.
    """
    options = deps.users.get_search(user_id)
    if not options:
        return "no_search", "run /search first"
    if not 1 <= n <= len(options):
        return "invalid", f"pick 1 to {len(options)}"

    option = options[n - 1]
    window_minutes = PRIORITY_WINDOW_MINUTES if user.get("priority") else REGULAR_WINDOW_MINUTES
    booking_id = booking_store.booking_id_for(option)

    result = deps.sched.schedule_or_book_now(
        telegram_user_id=user_id, booking_id=booking_id,
        option=option, window_minutes=window_minutes,
    )
    deps.bookings.create_booking(
        telegram_user_id=user_id, chat_id=str(chat_id), option=option,
        window_minutes=window_minutes,
        schedule_name=result.get("schedule_name", ""),
        fire_at=result.get("fire_at", "immediate"),
    )
    payload = {"option": option, "booking_id": booking_id,
               "fire_at": result.get("fire_at", "immediate")}
    return result["mode"], payload


def handle_pick(deps, chat_id: int, user_id: str, user: dict, text: str) -> None:
    parts = text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        deps.telegram.send_message(chat_id, "Usage: /pick <result number>")
        return
    status, payload = pick_option(deps, chat_id, user_id, user, int(parts[1]))
    if status == "no_search":
        deps.telegram.send_message(chat_id, "No recent search — run /search first.")
    elif status == "invalid":
        deps.telegram.send_message(chat_id, f"Invalid selection — {payload}.")
    elif status == "immediate":
        deps.telegram.send_message(chat_id, fmt.format_immediate(payload["option"]), parse_mode="HTML")
    else:
        deps.telegram.send_message(
            chat_id,
            fmt.format_scheduled(payload["option"], payload["fire_at"], payload["booking_id"]),
            parse_mode="HTML",
        )


def handle_bookings(deps, chat_id: int, user_id: str) -> None:
    items = deps.bookings.list_bookings(user_id)
    if not items:
        deps.telegram.send_message(chat_id, "No bookings yet. Schedule one with /search + /pick.")
        return
    deps.telegram.send_message(chat_id, fmt.format_bookings(items), parse_mode="HTML")


def handle_cancel(deps, chat_id: int, user_id: str, text: str) -> None:
    parts = text.split()
    if len(parts) != 2:
        deps.telegram.send_message(chat_id, "Usage: /cancel <booking id>  (ids are shown by /bookings)")
        return
    booking = deps.bookings.get_booking(user_id, parts[1])
    if not booking:
        deps.telegram.send_message(chat_id, f"Booking not found: {parts[1]}")
        return
    if booking.get("schedule_name"):
        deps.sched.delete_schedule(booking["schedule_name"])
    deps.bookings.delete_booking(user_id, booking["booking_id"])
    deps.telegram.send_message(chat_id, f"🗑 Cancelled: {booking.get('name', booking['booking_id'])}")


def dispatch(deps, chat_id: int, message_id: int, user_id: str,
             user: dict | None, text: str, help_text: str, lang: str = "en") -> None:
    """Route a command from an authorized (approved or admin) user."""
    if text.startswith("/login"):
        handle_login(deps, chat_id, message_id, user_id, text, lang)
        return
    if text.startswith("/status"):
        handle_status(deps, chat_id, user)
        return
    if text.startswith(("/search", "/pick", "/bookings", "/cancel")) and not _is_linked(user):
        deps.telegram.send_message(chat_id, "Link your gym account first: /login <email> <password>")
        return
    if text.startswith("/search"):
        handle_search(deps, chat_id, user_id, user, text)
    elif text.startswith("/pick"):
        handle_pick(deps, chat_id, user_id, user, text)
    elif text.startswith("/bookings"):
        handle_bookings(deps, chat_id, user_id)
    elif text.startswith("/cancel"):
        handle_cancel(deps, chat_id, user_id, text)
    else:
        deps.telegram.send_message(chat_id, help_text)
