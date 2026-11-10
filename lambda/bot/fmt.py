"""Telegram message formatting (HTML parse mode). Pure functions, no I/O.

Every user- or API-sourced string goes through esc() so stray <>& characters
can never break Telegram's HTML parser.
"""

import html
from datetime import datetime


def esc(value) -> str:
    return html.escape(str(value), quote=False)


def _day(date_str: str) -> str:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%a %d %b")
    except ValueError:
        return date_str


def _fullness(booked: int, capacity: int) -> str:
    pct = (booked / capacity) if capacity else 1.0
    icon = "🔴" if pct >= 0.9 else "🟡" if pct >= 0.7 else "🟢"
    return f"{icon} {booked}/{capacity}"


STATUS_ICONS = {"scheduled": "📅", "booked": "✅", "failed": "❌"}


def format_options(options: list[dict]) -> str:
    lines = [f"🔍 <b>{len(options)} classes found</b>"]
    current_day = None
    for o in options:
        if o["date"] != current_day:
            current_day = o["date"]
            lines += ["", f"📅 <b>{_day(current_day)}</b>"]
        lines.append(
            f" {o['idx']} · <code>{o['time']}</code> · <b>{esc(o['name'])}</b>"
            f" — {esc(o['studio'])} {_fullness(o['booked'], o['capacity'])}"
        )
    lines += ["", "👉 Book with /pick n"]
    return "\n".join(lines)


def format_bookings(items: list[dict]) -> str:
    blocks = []
    for b in items:
        icon = STATUS_ICONS.get(b.get("status"), "•")
        head = f"{icon} <b>{esc(b['name'])}</b> — {_day(b['date'])}, <code>{b['time']}</code>"
        detail = f"    id: <code>{b['booking_id']}</code>"
        if b.get("status") == "scheduled" and b.get("fire_at"):
            detail += f" · fires {b['fire_at']}"
        blocks.append(f"{head}\n{detail}")
    return "\n".join(blocks)


def format_scheduled(option: dict, fire_at: str, booking_id: str) -> str:
    return (
        f"📅 <b>Scheduled: {esc(option['name'])}</b>\n"
        f"{_day(option['date'])} at <code>{option['time']}</code> — {esc(option['studio'])}\n\n"
        f"🤖 The booker fires at <b>{fire_at}</b> (Europe/Rome),\n"
        f"3 minutes before the booking window opens.\n\n"
        f"Cancel with /cancel {booking_id}"
    )


def format_immediate(option: dict) -> str:
    return (
        f"⚡ <b>The booking window is already open!</b>\n"
        f"Booking <b>{esc(option['name'])}</b> ({_day(option['date'])} "
        f"<code>{option['time']}</code>) right now — the result lands here shortly."
    )


def format_status(user: dict) -> str:
    member_type = "PRIORITY" if user.get("priority") else "REGULAR"
    return (
        f"👤 <b>Linked account</b>\n"
        f"✉️ {esc(user['email'])}\n"
        f"🎫 {member_type} member · club {esc(user['club_id'])}"
    )
