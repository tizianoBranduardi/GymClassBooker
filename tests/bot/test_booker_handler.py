from unittest import mock

import pytest

import booker_handler

ADMIN_CHAT = "111111111"


@pytest.fixture
def deps():
    d = mock.Mock(spec=booker_handler.Deps)
    for name in ("telegram", "gym", "users", "bookings", "secrets"):
        setattr(d, name, mock.Mock())
    d.users.get_user.return_value = {"status": "approved"}
    return d


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ADMIN_CHAT_ID", ADMIN_CHAT)
    return monkeypatch


BOOKING = {
    "booking_id": "2026-10-30_1815_c1", "telegram_user_id": "777", "chat_id": "777",
    "class_id": "c1", "session_id": "s1", "date": "2026-10-30", "time": "18:15",
    "name": "Pilates", "window_minutes": 2880, "status": "scheduled",
}
CREDS = {"email": "a@b.c", "password": "pw", "api_key": "k", "club_id": "207"}


def run(deps, booking=None):
    deps.bookings.get_booking.return_value = dict(booking or BOOKING)
    deps.secrets.get_user_credentials.return_value = CREDS
    with mock.patch.object(booker_handler.core, "wait_until_open", return_value=True), \
         mock.patch.object(booker_handler.core, "book_with_retry") as retry:
        retry.side_effect = lambda once, **kw: once() == "success"
        return booker_handler.process({"telegram_user_id": booking["telegram_user_id"] if booking else "777",
                                       "booking_id": BOOKING["booking_id"]}, deps)


def messages_to(deps, chat):
    return [c.args[1] for c in deps.telegram.send_message.call_args_list
            if str(c.args[0]) == str(chat)]


def test_success_notifies_user_and_admin(deps):
    deps.gym.login.return_value = "tok"
    deps.gym.book_once.return_value = ("success", "")
    result = run(deps)

    assert result["status"] == "booked"
    deps.bookings.set_status.assert_called_once_with("777", BOOKING["booking_id"], "booked")
    assert any("✅" in m and "Pilates" in m for m in messages_to(deps, "777"))
    admin_msgs = messages_to(deps, ADMIN_CHAT)
    assert len(admin_msgs) == 1 and "Pilates" in admin_msgs[0] and "✅" in admin_msgs[0]


def test_failure_report_includes_reason(deps):
    deps.gym.login.return_value = "tok"
    deps.gym.book_once.return_value = ("rejected", "CLASS_FULL")
    result = run(deps)

    assert result["status"] == "failed"
    user_msgs = messages_to(deps, "777")
    assert any("❌" in m and "CLASS_FULL" in m for m in user_msgs)
    admin_msgs = messages_to(deps, ADMIN_CHAT)
    assert len(admin_msgs) == 1 and "CLASS_FULL" in admin_msgs[0]


def test_admin_booking_gets_single_message(deps):
    booking = {**BOOKING, "telegram_user_id": ADMIN_CHAT, "chat_id": ADMIN_CHAT}
    deps.gym.login.return_value = "tok"
    deps.gym.book_once.return_value = ("success", "")
    run(deps, booking=booking)
    assert len(messages_to(deps, ADMIN_CHAT)) == 1  # no duplicate admin report


def test_login_failure_reason(deps):
    deps.bookings.get_booking.return_value = dict(BOOKING)
    deps.secrets.get_user_credentials.return_value = CREDS
    deps.gym.login.return_value = None
    result = booker_handler.process({"telegram_user_id": "777",
                                     "booking_id": BOOKING["booking_id"]}, deps)
    assert result["status"] == "failed"
    assert any("login" in m.lower() for m in messages_to(deps, ADMIN_CHAT))


def test_crash_is_reported_to_admin(deps):
    deps.bookings.get_booking.return_value = dict(BOOKING)
    deps.secrets.get_user_credentials.side_effect = RuntimeError("secrets exploded")
    result = booker_handler.process({"telegram_user_id": "777",
                                     "booking_id": BOOKING["booking_id"]}, deps)
    assert result["status"] == "crashed"
    deps.bookings.set_status.assert_called_once_with("777", BOOKING["booking_id"], "failed")
    assert any("secrets exploded" in m for m in messages_to(deps, ADMIN_CHAT))


def test_missing_booking_is_noop(deps):
    deps.bookings.get_booking.return_value = None
    result = booker_handler.process({"telegram_user_id": "777", "booking_id": "x"}, deps)
    assert result["status"] == "skipped"
    deps.gym.login.assert_not_called()


def test_already_processed_booking_is_noop(deps):
    deps.bookings.get_booking.return_value = {**BOOKING, "status": "booked"}
    result = booker_handler.process({"telegram_user_id": "777",
                                     "booking_id": BOOKING["booking_id"]}, deps)
    assert result["status"] == "skipped"
