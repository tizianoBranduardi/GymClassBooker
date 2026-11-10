from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import booking_core as core

ROME = ZoneInfo("Europe/Rome")


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def rome(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=ROME)


# --- window math -----------------------------------------------------------------

def test_window_opens_at_is_rome_aware():
    opens = core.window_opens_at("2026-10-28", "18:15", 2880)
    assert opens == rome(2026, 10, 26, 18, 15)
    assert opens.tzinfo is not None


def test_wait_until_window_opens_waits(monkeypatch):
    clock = FakeClock(rome(2026, 10, 26, 18, 14, 30))
    opens = rome(2026, 10, 26, 18, 15)
    assert core.wait_until_open(opens, now_fn=clock, sleep_fn=clock.sleep)
    assert clock.now >= opens


def test_wait_returns_immediately_when_already_open():
    clock = FakeClock(rome(2026, 10, 26, 19, 0))
    assert core.wait_until_open(rome(2026, 10, 26, 18, 15), now_fn=clock, sleep_fn=clock.sleep)
    assert clock.now == rome(2026, 10, 26, 19, 0)  # no sleeping


# --- retry loop --------------------------------------------------------------------

def run_retry(outcomes, window_s=90):
    clock = FakeClock(rome(2026, 10, 26, 18, 15))
    calls = {"n": 0}

    def once():
        out = outcomes[min(calls["n"], len(outcomes) - 1)]
        calls["n"] += 1
        return out

    ok = core.book_with_retry(once, retry_window_seconds=window_s,
                              retry_interval_seconds=0.5,
                              now_fn=clock, sleep_fn=clock.sleep)
    return ok, calls["n"]


def test_retry_books_after_too_early():
    ok, n = run_retry(["too_early", "too_early", "success"])
    assert ok and n == 3


def test_retry_stops_on_rejection():
    ok, n = run_retry(["rejected"])
    assert not ok and n == 1


def test_retry_gives_up_at_deadline():
    ok, n = run_retry(["too_early"], window_s=5)
    assert not ok and 8 <= n <= 13
