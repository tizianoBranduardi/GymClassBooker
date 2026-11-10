import json
from unittest import mock

import pytest

import handler

ADMIN = 111111111
USER = 777000111
LINKED = {"status": "approved", "email": "a@b.c", "club_id": "207",
          "priority": False, "chat_id": str(USER)}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_ID", str(ADMIN))
    monkeypatch.setenv("DEFAULT_CLUB_ID", "207")
    monkeypatch.setenv("WEBHOOK_SECRET_ID", "gym-booker/webhook-secret")
    return monkeypatch


@pytest.fixture
def deps():
    d = mock.Mock(spec=handler.Deps)
    for name in ("telegram", "gym", "users", "bookings", "sched"):
        setattr(d, name, mock.Mock())
    return d


def tg_event(text, user_id=USER):
    update = {"message": {"message_id": 10, "from": {"id": user_id, "first_name": "T"},
                          "chat": {"id": user_id, "type": "private"}, "text": text}}
    return {"headers": {"x-telegram-bot-api-secret-token": "s3cret"},
            "body": json.dumps(update)}


def run(deps, event):
    with mock.patch.object(handler, "get_webhook_secret", return_value="s3cret"):
        return handler.process_event(event, deps)


def reply(deps, i=0):
    return deps.telegram.send_message.call_args_list[i].args[1]


SAMPLE_OPTIONS = [
    {"idx": 1, "date": "2026-10-30", "time": "18:15", "name": "Pilates", "studio": "Main",
     "booked": 5, "capacity": 20, "class_id": "c1", "session_id": "s1"},
    {"idx": 2, "date": "2026-10-31", "time": "19:00", "name": "Spin", "studio": "Cycle",
     "booked": 19, "capacity": 20, "class_id": "c2", "session_id": "s2"},
]


# --- /search ---------------------------------------------------------------------

def test_search_requires_linked_account(env, deps):
    deps.users.get_user.return_value = {"status": "approved"}  # no email
    run(deps, tg_event("/search 2026-10-30 18:00"))
    assert "/login" in reply(deps)
    deps.gym.search_options.assert_not_called()


def test_search_lists_and_saves_options(env, deps):
    deps.users.get_user.return_value = LINKED
    deps.gym.search_options.return_value = SAMPLE_OPTIONS
    run(deps, tg_event("/search 2026-10-30 18:00"))

    deps.gym.search_options.assert_called_once()
    deps.users.save_search.assert_called_once_with(str(USER), SAMPLE_OPTIONS)
    out = reply(deps)
    assert "Pilates" in out and "Spin" in out and "/pick" in out
    # HTML mode must be used for the formatted list
    assert deps.telegram.send_message.call_args.kwargs.get("parse_mode") == "HTML"


def test_search_bad_args_shows_usage(env, deps):
    deps.users.get_user.return_value = LINKED
    run(deps, tg_event("/search tomorrow"))
    assert "usage" in reply(deps).lower()


def test_search_no_results(env, deps):
    deps.users.get_user.return_value = LINKED
    deps.gym.search_options.return_value = []
    run(deps, tg_event("/search 2026-10-30 18:00"))
    assert "no classes" in reply(deps).lower()


# --- /pick -----------------------------------------------------------------------

def test_pick_schedules_future_booking(env, deps):
    deps.users.get_user.return_value = LINKED
    deps.users.get_search.return_value = SAMPLE_OPTIONS
    deps.sched.schedule_or_book_now.return_value = {"mode": "scheduled",
                                                    "fire_at": "2026-10-28 18:12"}
    run(deps, tg_event("/pick 1"))

    deps.bookings.create_booking.assert_called_once()
    kw = deps.bookings.create_booking.call_args.kwargs
    assert kw["telegram_user_id"] == str(USER)
    assert kw["option"]["class_id"] == "c1"
    assert kw["window_minutes"] == 2880  # regular member
    out = reply(deps)
    assert "Pilates" in out and "2026-10-28 18:12" in out


def test_pick_books_immediately_when_window_open(env, deps):
    deps.users.get_user.return_value = LINKED
    deps.users.get_search.return_value = SAMPLE_OPTIONS
    deps.sched.schedule_or_book_now.return_value = {"mode": "immediate"}
    run(deps, tg_event("/pick 2"))
    assert "already open" in reply(deps).lower()


def test_pick_without_search(env, deps):
    deps.users.get_user.return_value = LINKED
    deps.users.get_search.return_value = None
    run(deps, tg_event("/pick 1"))
    assert "/search" in reply(deps)
    deps.bookings.create_booking.assert_not_called()


def test_pick_out_of_range(env, deps):
    deps.users.get_user.return_value = LINKED
    deps.users.get_search.return_value = SAMPLE_OPTIONS
    run(deps, tg_event("/pick 7"))
    assert "invalid" in reply(deps).lower()


def test_pick_priority_member_uses_long_window(env, deps):
    deps.users.get_user.return_value = {**LINKED, "priority": True}
    deps.users.get_search.return_value = SAMPLE_OPTIONS
    deps.sched.schedule_or_book_now.return_value = {"mode": "scheduled", "fire_at": "x"}
    run(deps, tg_event("/pick 1"))
    assert deps.bookings.create_booking.call_args.kwargs["window_minutes"] == 8640


# --- /bookings & /cancel -----------------------------------------------------------

def test_bookings_lists_scheduled(env, deps):
    deps.users.get_user.return_value = LINKED
    deps.bookings.list_bookings.return_value = [
        {"booking_id": "2026-10-30_1815_c1", "name": "Pilates", "date": "2026-10-30",
         "time": "18:15", "status": "scheduled"},
    ]
    run(deps, tg_event("/bookings"))
    out = reply(deps)
    assert "Pilates" in out and "2026-10-30_1815_c1" in out


def test_bookings_empty(env, deps):
    deps.users.get_user.return_value = LINKED
    deps.bookings.list_bookings.return_value = []
    run(deps, tg_event("/bookings"))
    assert "no bookings" in reply(deps).lower()


def test_cancel_deletes_schedule_and_record(env, deps):
    deps.users.get_user.return_value = LINKED
    deps.bookings.get_booking.return_value = {"booking_id": "2026-10-30_1815_c1",
                                              "schedule_name": "gym-x", "name": "Pilates"}
    run(deps, tg_event("/cancel 2026-10-30_1815_c1"))
    deps.sched.delete_schedule.assert_called_once_with("gym-x")
    deps.bookings.delete_booking.assert_called_once_with(str(USER), "2026-10-30_1815_c1")
    assert "cancelled" in reply(deps).lower()


def test_cancel_unknown_id(env, deps):
    deps.users.get_user.return_value = LINKED
    deps.bookings.get_booking.return_value = None
    run(deps, tg_event("/cancel nope"))
    deps.sched.delete_schedule.assert_not_called()
    assert "not found" in reply(deps).lower()
