"""Natural-language layer: Bedrock Converse with booking tools.

Handles free-text messages from linked users ("book me a pilates class
tomorrow evening"). The model resolves relative dates from today's date in
the system prompt, must ask instead of assuming a missing date/time, and must
get explicit confirmation before scheduling a booking.
"""

import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import boto3

import commands

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

MAX_TOOL_ROUNDS = 6
MAX_HISTORY = 8  # stored conversation turns (user+assistant text only)

TOOLS = [
    {"toolSpec": {
        "name": "search_classes",
        "description": ("Search bookable classes at the user's club: a 3-hour window "
                        "from the given time, on the given date and each of the next 3 days. "
                        "Returns a numbered list of options."),
        "inputSchema": {"json": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "Target date, YYYY-MM-DD"},
                "time": {"type": "string", "description": "Start of the 3h window, HH:MM 24h"},
                "name_filter": {"type": "string",
                                "description": ("Optional case-insensitive substring to keep only "
                                                "matching class names (e.g. 'pilates'). "
                                                "Omit to show all classes.")},
            },
            "required": ["date", "time"],
        }},
    }},
    {"toolSpec": {
        "name": "schedule_booking",
        "description": ("Schedule the booking of option <number> from the latest search. "
                        "Only call after the user explicitly confirmed that exact class."),
        "inputSchema": {"json": {
            "type": "object",
            "properties": {"number": {"type": "integer", "description": "Option number from the latest search"}},
            "required": ["number"],
        }},
    }},
    {"toolSpec": {
        "name": "list_bookings",
        "description": "List the user's scheduled/booked/failed bookings.",
        "inputSchema": {"json": {"type": "object", "properties": {}}},
    }},
    {"toolSpec": {
        "name": "cancel_booking",
        "description": "Cancel a scheduled booking by its booking id (shown by list_bookings).",
        "inputSchema": {"json": {
            "type": "object",
            "properties": {"booking_id": {"type": "string"}},
            "required": ["booking_id"],
        }},
    }},
]


def _now_rome() -> datetime:
    return datetime.now(tz=ZoneInfo("Europe/Rome"))


def _system_prompt(user: dict) -> list[dict]:
    now = _now_rome()
    return [{"text": (
        "You are a Telegram assistant that books gym classes for the user.\n"
        f"Today is {now.strftime('%A %Y-%m-%d')} and the current time is {now.strftime('%H:%M')} "
        "(Europe/Rome). Resolve every relative date the user gives "
        "('tomorrow', 'next monday', 'in two days') from this date.\n"
        "Rules:\n"
        "- NEVER assume a date or time the user did not state. If the date or the time is "
        "missing or ambiguous, ask one short clarifying question instead of calling a tool.\n"
        "- search_classes needs both a date and a time. Pass name_filter only when the user "
        "asked for a specific kind of class; omit it when they want all classes.\n"
        "- Before calling schedule_booking you MUST have shown the user the numbered options "
        "and received an explicit confirmation of which number to book.\n"
        "- Language: ALWAYS answer in the same language as the user's latest message. "
        "If the user writes Italian, answer in Italian.\n"
        "- Style: brief and playful, with catchy slang and a few emoji. Max 5 short lines. "
        "Example for an Italian question 'Cosa puoi fare?': "
        "'Ehi! 👋 Ti trovo corsi e ti prenoto il posto 💪 Prova: <i>corsi domani alle 18</i> 🔥'\n"
        "- Formatting: Telegram HTML. Use only <b>bold</b>, <i>italic</i> and <code>times or ids</code>. "
        "Write '&' as 'and'. Never use markdown (no ** or #).\n"
        "- Class lists: one class per line, like '1️⃣ <code>18:15</code> <b>Pilates</b> · Main · 5/20 spots', "
        "grouped under a bold day heading. Present them exactly as numbered by the tool.\n"
        f"- The user's club id is {user.get('club_id', 'unknown')}; bookings are made with "
        "their own linked account."
    )}]


# --- tool execution -----------------------------------------------------------

def _run_search(deps, user_id: str, user: dict, args: dict) -> str:
    options = deps.gym.search_options(
        telegram_user_id=user_id, club_id=user.get("club_id"),
        target_date=args["date"], target_time=args["time"],
    )
    if options is None:
        return "ERROR: gym login failed; tell the user to re-link with /login."
    name_filter = (args.get("name_filter") or "").strip().lower()
    if name_filter:
        options = [o for o in options if name_filter in o["name"].lower()]
        for i, o in enumerate(options, 1):
            o["idx"] = i
    if not options:
        return "No classes found for these criteria."
    deps.users.save_search(user_id, options)
    compact = [{"n": o["idx"], "date": o["date"], "time": o["time"], "name": o["name"],
                "studio": o["studio"], "spots": f"{o['booked']}/{o['capacity']}"}
               for o in options]
    return json.dumps({"options": compact})


def _run_schedule(deps, chat_id: int, user_id: str, user: dict, args: dict) -> str:
    status, payload = commands.pick_option(deps, chat_id, user_id, user, int(args["number"]))
    if status == "no_search":
        return "ERROR: no recent search; run search_classes first."
    if status == "invalid":
        return f"ERROR: invalid option number ({payload})."
    option = payload["option"]
    label = f"{option['name']} on {option['date']} {option['time']}"
    if status == "immediate":
        return f"Booking window already open: booking {label} right now; the result will arrive as a separate message."
    return f"Scheduled: {label}. The booker fires at {payload['fire_at']} (Europe/Rome), id {payload['booking_id']}."


def _run_list(deps, user_id: str) -> str:
    items = deps.bookings.list_bookings(user_id)
    if not items:
        return "No bookings."
    return json.dumps([{"id": b["booking_id"], "name": b["name"], "date": b["date"],
                        "time": b["time"], "status": b.get("status")} for b in items])


def _run_cancel(deps, user_id: str, args: dict) -> str:
    booking = deps.bookings.get_booking(user_id, args["booking_id"])
    if not booking:
        return f"ERROR: booking {args['booking_id']} not found."
    if booking.get("schedule_name"):
        deps.sched.delete_schedule(booking["schedule_name"])
    deps.bookings.delete_booking(user_id, booking["booking_id"])
    return f"Cancelled {booking.get('name', booking['booking_id'])}."


def _execute_tool(deps, chat_id: int, user_id: str, user: dict, name: str, args: dict) -> str:
    try:
        if name == "search_classes":
            return _run_search(deps, user_id, user, args)
        if name == "schedule_booking":
            return _run_schedule(deps, chat_id, user_id, user, args)
        if name == "list_bookings":
            return _run_list(deps, user_id)
        if name == "cancel_booking":
            return _run_cancel(deps, user_id, args)
        return f"ERROR: unknown tool {name}"
    except Exception as e:
        logger.exception("Tool %s failed", name)
        return f"ERROR: tool {name} failed: {e}"


# --- main loop -------------------------------------------------------------------

def _guardrail_config() -> dict | None:
    """Bedrock Guardrail attached to every model call, if configured.
    A blocked request comes back as a normal reply carrying the guardrail's message."""
    guardrail_id = os.environ.get("BEDROCK_GUARDRAIL_ID", "").strip()
    if not guardrail_id:
        return None
    return {
        "guardrailIdentifier": guardrail_id,
        "guardrailVersion": os.environ.get("BEDROCK_GUARDRAIL_VERSION", "DRAFT"),
        "trace": "disabled",
    }


def _markdown_to_html(text: str) -> str:
    """Models often ignore the HTML instruction; convert the common markdown
    so it still reads well in Telegram."""
    lines = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("- ", "* ")):
            line = line[: len(line) - len(stripped)] + "• " + stripped[2:]
        lines.append(line)
    out = "\n".join(lines)
    parts = out.split("**")
    # odd-indexed parts sit between a matched pair of ** markers
    return "".join(p if i % 2 == 0 else f"<b>{p}</b>" for i, p in enumerate(parts))


def _plain(text: str) -> str:
    """Strip the allowed HTML tags, for the plain-text fallback."""
    for tag in ("<b>", "</b>", "<i>", "</i>", "<code>", "</code>"):
        text = text.replace(tag, "")
    return text.replace("&amp;", "&")


def _send_reply(deps, chat_id: int, text: str) -> None:
    """Send formatted HTML; if Telegram rejects the markup, resend as plain text."""
    text = _markdown_to_html(text)
    try:
        deps.telegram.send_message(chat_id, text, parse_mode="HTML")
    except Exception:
        logger.warning("Telegram rejected the HTML reply; resending as plain text")
        deps.telegram.send_message(chat_id, _plain(text))


def handle(deps, chat_id: int, user_id: str, user: dict, text: str, bedrock=None) -> None:
    bedrock = bedrock or boto3.client("bedrock-runtime")
    history = deps.users.get_conv(user_id) or []
    user_turn = {"role": "user", "content": [{"text": text}]}
    messages = history + [user_turn]
    guardrail = _guardrail_config()

    try:
        out_message = None
        for _ in range(MAX_TOOL_ROUNDS):
            kwargs = {}
            if guardrail:
                kwargs["guardrailConfig"] = guardrail
            resp = bedrock.converse(
                modelId=os.environ["BEDROCK_MODEL_ID"],
                system=_system_prompt(user),
                messages=messages,
                toolConfig={"tools": TOOLS},
                inferenceConfig={"maxTokens": 300},
                **kwargs,
            )
            out_message = resp["output"]["message"]
            messages.append(out_message)
            if resp["stopReason"] != "tool_use":
                break
            results = []
            for block in out_message["content"]:
                if "toolUse" in block:
                    tu = block["toolUse"]
                    result = _execute_tool(deps, chat_id, user_id, user, tu["name"], tu.get("input") or {})
                    results.append({"toolResult": {"toolUseId": tu["toolUseId"],
                                                   "content": [{"text": result}]}})
            messages.append({"role": "user", "content": results})
        final_text = "".join(b.get("text", "") for b in (out_message or {}).get("content", [])).strip()
        if not final_text:
            final_text = "🤖 Done — ask /bookings for the current state."
    except Exception:
        logger.exception("Assistant failed")
        deps.telegram.send_message(chat_id, "😵 Sorry, something went wrong — please try again (or use /search).")
        return

    _send_reply(deps, chat_id, final_text)
    new_history = (history + [user_turn,
                              {"role": "assistant", "content": [{"text": final_text}]}])[-MAX_HISTORY:]
    # history must start with a user turn for the Converse API
    while new_history and new_history[0]["role"] != "user":
        new_history.pop(0)
    deps.users.save_conv(user_id, new_history)
