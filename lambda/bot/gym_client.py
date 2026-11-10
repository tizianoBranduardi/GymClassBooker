"""Gym booking API client used by both the bot (validate/search) and the booker."""

import base64
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)
TIMEOUT = 10
SHOW_HOURS_AFTER_TARGET = 3
DAYS_TO_SHOW = 4  # target date + the following 3 days


def _base_url() -> str:
    return os.environ["GYM_API_BASE_URL"]


def _headers(api_key: str) -> dict[str, str]:
    return {
        "X-API-Key": api_key,
        "X-App-ID": os.environ["GYM_APP_ID"],
        "X-Region": "IT",
        "X-Va-Region": "ITA",
        "X-App-Version": "7.6.1 (726083014)",
        "lang_pref": "it",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Ktor client",
        "Accept-Language": "it-IT,it;q=0.9",
    }


def _claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        return {k: str(v) for k, v in json.loads(base64.urlsafe_b64decode(payload)).items()}
    except Exception:
        return {}


def _auth_headers(token: str, api_key: str) -> dict[str, str]:
    c = _claims(token)
    return {**_headers(api_key), "X-Auth-Token": token,
            "X-Member-Id": c.get("mid", ""), "X-Member-External-Id": c.get("xid", "")}


def login(email: str, password: str, api_key: str) -> Optional[str]:
    try:
        resp = requests.post(f"{_base_url()}/sessions", headers=_headers(api_key),
                             json={"memberId": email, "password": password}, timeout=TIMEOUT)
    except requests.RequestException:
        logger.exception("gym login request failed")
        return None
    if resp.status_code != 200:
        logger.info("gym login rejected: HTTP %s", resp.status_code)
        return None
    return resp.json().get("token")


def validate(email: str, password: str) -> dict:
    """Credential check for /login. Uses the app-level GYM_API_KEY env var."""
    token = login(email, password, os.environ["GYM_API_KEY"])
    if not token:
        return {"ok": False}
    return {"ok": True, "priority": _claims(token).get("pp") == "true"}


def search_options(*, telegram_user_id: str, club_id: str | None,
                   target_date: str, target_time: str) -> list[dict] | None:
    """Handler-facing search: load the user's credentials, login, list classes.

    Returns None when the stored credentials no longer work (prompt re-/login),
    [] when the login worked but nothing matches.
    """
    import booking_store  # local import: booking_store has no gym_client dependency

    creds = booking_store.get_user_credentials(str(telegram_user_id))
    token = login(creds["email"], creds["password"], creds["api_key"])
    if not token:
        return None
    return list_classes(token, creds["api_key"], club_id or creds.get("club_id", ""),
                        target_date, target_time)


def list_classes(token: str, api_key: str, club_id: str,
                 target_date: str, target_time: str) -> list[dict]:
    """Bookable classes between target_time and +3h on the target date and
    each of the following 3 days."""
    target_dt = datetime.strptime(f"{target_date} {target_time}", "%Y-%m-%d %H:%M")
    end_date = (target_dt + timedelta(days=DAYS_TO_SHOW - 1)).strftime("%Y-%m-%d")
    try:
        resp = requests.get(f"{_base_url()}/clubs/{club_id}/classes",
                            headers=_auth_headers(token, api_key),
                            params={"startDate": target_date, "endDate": end_date},
                            timeout=TIMEOUT)
        resp.raise_for_status()
        days = resp.json()
    except Exception:
        logger.exception("gym class search failed")
        return []

    valid_dates = {(target_dt.date() + timedelta(days=o)) for o in range(DAYS_TO_SHOW)}
    options: list[dict] = []
    for day in days or []:
        try:
            day_date = datetime.strptime(day["date"], "%Y-%m-%d").date()
        except Exception:
            continue
        if day_date not in valid_dates:
            continue
        window_start = datetime.combine(day_date, target_dt.time())
        window_end = window_start + timedelta(hours=SHOW_HOURS_AFTER_TARGET)
        for c in day.get("classes", []):
            if c.get("bookableStatus") == "Full":
                continue
            try:
                dt = datetime.strptime(f"{day['date']} {c['classTime']}", "%Y-%m-%d %H:%M:%S")
                if not (window_start <= dt <= window_end):
                    continue
                options.append({
                    "idx": len(options) + 1,
                    "date": day["date"],
                    "time": c["classTime"][:5],
                    "name": c["name"],
                    "studio": c.get("studioName", "N/A"),
                    "booked": int(c.get("bookingCount", 0)),
                    "capacity": int(c.get("bookingCapacity", 0)),
                    "class_id": str(c["id"]),
                    "session_id": str(c["sessionId"]),
                })
            except Exception:
                continue
    return options


def book_once(token: str, api_key: str, club_id: str,
              class_id: str, session_id: str, class_date: str) -> tuple[str, str]:
    """One booking attempt. Returns (outcome, detail):
    outcome: success | too_early | error | rejected; detail: the API's reason."""
    url = f"{_base_url()}/clubs/{club_id}/classes/{class_id}/sessions/{session_id}?date={class_date}"
    try:
        resp = requests.post(url, headers=_auth_headers(token, api_key), timeout=TIMEOUT)
    except requests.RequestException as e:
        logger.exception("Booking attempt failed")
        return "error", f"network error: {e.__class__.__name__}"
    if resp.status_code in (200, 201):
        logger.info("Booking successful (HTTP %s)", resp.status_code)
        return "success", ""
    try:
        body = resp.json()
    except Exception:
        body = {}
    detail = str(body.get("errorMessage") or body or resp.text[:120] or f"HTTP {resp.status_code}")
    if detail == "TOO_EARLY_TO_BOOK":
        return "too_early", detail
    if resp.status_code >= 500:
        logger.warning("Server error HTTP %s, will retry", resp.status_code)
        return "error", f"HTTP {resp.status_code}"
    logger.warning("Booking rejected: HTTP %s %s", resp.status_code, detail)
    return "rejected", detail
