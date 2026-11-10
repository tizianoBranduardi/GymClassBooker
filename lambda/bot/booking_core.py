"""Window math, precise wait and the booking retry loop.

Uses explicit Europe/Rome timezone handling because Lambda runs in UTC.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ROME = ZoneInfo("Europe/Rome")
POLL_INTERVAL_SECONDS = 0.5


def class_datetime(date: str, hhmm: str) -> datetime:
    """Class start as an aware Europe/Rome datetime."""
    return datetime.strptime(f"{date} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=ROME)


def window_opens_at(date: str, hhmm: str, window_minutes: int) -> datetime:
    return class_datetime(date, hhmm) - timedelta(minutes=window_minutes)


def now_rome() -> datetime:
    return datetime.now(tz=ROME)


def wait_until_open(
    window_opens: datetime,
    now_fn: Callable[[], datetime] = now_rome,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> bool:
    """Poll every 500ms until window_opens. Returns immediately if already open."""
    now = now_fn()
    remaining = (window_opens - now).total_seconds()
    if remaining <= 0:
        logger.info("Window already open (opened %.0fs ago)", -remaining)
        return True

    logger.info("Waiting %.1fs until window opens at %s", remaining, window_opens.isoformat())
    last_log = now
    while True:
        now = now_fn()
        remaining = (window_opens - now).total_seconds()
        if remaining <= 0:
            logger.info("WINDOW OPEN at %s (%.0fms past)", now.isoformat(), -remaining * 1000)
            return True
        if (now - last_log).total_seconds() >= 10:
            logger.info("Still waiting... %.1fs", remaining)
            last_log = now
        sleep_fn(POLL_INTERVAL_SECONDS)


def book_with_retry(
    book_once: Callable[[], str],
    retry_window_seconds: int = 90,
    retry_interval_seconds: float = 0.5,
    now_fn: Callable[[], datetime] = now_rome,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> bool:
    """Call book_once() until success/definitive rejection or the time budget ends.

    book_once returns "success" | "too_early" | "error" | "rejected".
    Retries cover TOO_EARLY_TO_BOOK (server clock ahead) and transient errors.
    """
    deadline = now_fn() + timedelta(seconds=retry_window_seconds)
    attempt = 0
    while True:
        attempt += 1
        outcome = book_once()
        if outcome == "success":
            if attempt > 1:
                logger.info("Booked after %d attempts", attempt)
            return True
        if outcome == "rejected":
            return False
        if now_fn() >= deadline:
            logger.error("Giving up after %d attempts (%ds budget)", attempt, retry_window_seconds)
            return False
        if outcome == "too_early" and attempt % 10 == 1:
            logger.info("TOO_EARLY_TO_BOOK (attempt %d), retrying...", attempt)
        sleep_fn(retry_interval_seconds)
