"""Enrollment behaviour against an in-memory user store (no mocks of the logic itself)."""

import json
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

import admin
import enrollment
import handler

ADMIN = "111111111"
REQUESTER = "777000111"
NOW = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)


class FakeUsers:
    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.secrets: set[str] = set()

    def get_user(self, uid):
        row = self.rows.get(str(uid))
        return dict(row) if row else None

    def count_pending(self):
        return sum(1 for r in self.rows.values() if r.get("status") == "pending")

    def create_pending(self, *, telegram_user_id, chat_id, first_name, last_name="",
                       username="", language="en"):
        self.rows[telegram_user_id] = {
            "telegram_user_id": telegram_user_id, "chat_id": chat_id, "first_name": first_name,
            "last_name": last_name, "username": username, "language": language,
            "status": "pending", "requested_at": NOW.isoformat(),
        }

    def touch_request(self, uid):
        self.rows[str(uid)]["requested_at"] = NOW.isoformat()

    def set_status(self, uid, status):
        self.rows[str(uid)]["status"] = status

    def revoke_access(self, uid):
        self.secrets.discard(str(uid))
        row = self.rows[str(uid)]
        row["status"] = "denied"
        for k in ("email", "club_id", "priority", "secret_name", "login_step", "pending_email"):
            row.pop(k, None)

    def delete_user(self, uid):
        self.secrets.discard(str(uid))
        self.rows.pop(str(uid), None)

    def set_login_step(self, uid, step, email=None):
        row = self.rows[str(uid)]
        if step is None:
            row.pop("login_step", None)
            row.pop("pending_email", None)
        else:
            row["login_step"] = step
            if email:
                row["pending_email"] = email

    def link_credentials(self, *, telegram_user_id, chat_id, email, password, club_id, priority):
        self.secrets.add(str(telegram_user_id))
        row = self.rows.setdefault(str(telegram_user_id), {"telegram_user_id": str(telegram_user_id)})
        row.update({"status": row.get("status", "approved"), "chat_id": chat_id, "email": email,
                    "club_id": club_id, "priority": priority})
        row.pop("login_step", None)
        row.pop("pending_email", None)

    def list_users(self):
        return [dict(r) for r in self.rows.values()]


class FakeBookings:
    def __init__(self):
        self.rows: list[dict] = []

    def list_bookings(self, uid):
        return [dict(b) for b in self.rows if b["telegram_user_id"] == str(uid)]

    def set_status(self, uid, bid, status):
        for b in self.rows:
            if b["telegram_user_id"] == str(uid) and b["booking_id"] == bid:
                b["status"] = status


class FakeTelegram:
    def __init__(self):
        self.sent: list[dict] = []
        self.answers: list[str] = []
        self.deleted: list[int] = []

    def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append({"chat": str(chat_id), "text": text, "markup": reply_markup})
        return {"message_id": len(self.sent)}

    def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)

    def answer_callback_query(self, callback_id, text=""):
        self.answers.append(text)

    def to(self, chat):
        return [m for m in self.sent if m["chat"] == str(chat)]


class FakeSched:
    def __init__(self):
        self.deleted: list[str] = []

    def delete_schedule(self, name):
        self.deleted.append(name)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_ID", ADMIN)
    monkeypatch.setenv("DEFAULT_CLUB_ID", "207")
    monkeypatch.setenv("WEBHOOK_SECRET_ID", "x")
    return monkeypatch


@pytest.fixture
def deps():
    d = handler.Deps(telegram=FakeTelegram(), gym=mock.Mock(), users=FakeUsers(),
                     bookings=FakeBookings(), sched=FakeSched())
    d.gym.validate.return_value = {"ok": True, "priority": False}
    return d


def msg(text, user_id=REQUESTER, message_id=10, first="Mario", last="Rossi", username="mrossi",
        lang="it"):
    frm = {"id": int(user_id), "first_name": first, "language_code": lang}
    if last is not None:
        frm["last_name"] = last
    if username is not None:
        frm["username"] = username
    return {"message": {"message_id": message_id, "from": frm,
                        "chat": {"id": int(user_id), "type": "private"}, "text": text}}


def run(deps, update):
    with mock.patch.object(handler, "get_webhook_secret", return_value="s3cret"), \
         mock.patch.object(enrollment, "now_utc", return_value=NOW):
        return handler.process_event(
            {"headers": {"x-telegram-bot-api-secret-token": "s3cret"}, "body": json.dumps(update)}, deps)


def callback(data, from_id=ADMIN):
    return {"callback_query": {"id": "cb1", "from": {"id": int(from_id)},
                               "data": data, "message": {"chat": {"id": int(from_id)}}}}


# --- request capture ---------------------------------------------------------------

def test_request_stores_full_name_and_username(env, deps):
    run(deps, msg("/start"))
    row = deps.users.rows[REQUESTER]
    assert row["first_name"] == "Mario" and row["last_name"] == "Rossi" and row["username"] == "mrossi"


def test_admin_gets_name_and_buttons_only(env, deps):
    run(deps, msg("/start"))
    admin_msgs = deps.telegram.to(ADMIN)
    assert len(admin_msgs) == 1
    assert "Mario Rossi" in admin_msgs[0]["text"] and "@mrossi" in admin_msgs[0]["text"]
    assert REQUESTER in admin_msgs[0]["text"]
    buttons = admin_msgs[0]["markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == [f"approve:{REQUESTER}", f"deny:{REQUESTER}"]
    assert deps.telegram.to(REQUESTER)  # the requester gets an acknowledgement, not the buttons
    assert all(m["markup"] is None for m in deps.telegram.to(REQUESTER))


def test_requester_reply_is_in_their_language(env, deps):
    run(deps, msg("/start", lang="it"))
    assert "richiesta" in deps.telegram.to(REQUESTER)[0]["text"].lower()


def test_request_without_last_name_or_username(env, deps):
    run(deps, msg("/start", last=None, username=None))
    assert deps.users.rows[REQUESTER]["last_name"] == ""
    assert "Mario" in deps.telegram.to(ADMIN)[0]["text"]


# --- pending lifecycle ----------------------------------------------------------------

def test_pending_user_within_cooldown_is_not_renotified(env, deps):
    run(deps, msg("/start"))
    deps.telegram.sent.clear()
    with mock.patch.object(handler, "get_webhook_secret", return_value="s3cret"), \
         mock.patch.object(enrollment, "now_utc", return_value=NOW + timedelta(minutes=5)):
        handler.process_event({"headers": {"x-telegram-bot-api-secret-token": "s3cret"},
                               "body": json.dumps(msg("ciao"))}, deps)
    assert not deps.telegram.to(ADMIN)


def test_pending_user_after_cooldown_triggers_reminder(env, deps):
    run(deps, msg("/start"))
    deps.telegram.sent.clear()
    with mock.patch.object(handler, "get_webhook_secret", return_value="s3cret"), \
         mock.patch.object(enrollment, "now_utc", return_value=NOW + timedelta(hours=2)):
        handler.process_event({"headers": {"x-telegram-bot-api-secret-token": "s3cret"},
                               "body": json.dumps(msg("ciao"))}, deps)
    assert any("Reminder" in m["text"] or "reminder" in m["text"] for m in deps.telegram.to(ADMIN))


def test_expired_request_is_recreated_as_new(env, deps):
    run(deps, msg("/start"))
    deps.users.rows[REQUESTER]["requested_at"] = (NOW - timedelta(days=8)).isoformat()
    with mock.patch.object(handler, "get_webhook_secret", return_value="s3cret"), \
         mock.patch.object(enrollment, "now_utc", return_value=NOW):
        handler.process_event({"headers": {"x-telegram-bot-api-secret-token": "s3cret"},
                               "body": json.dumps(msg("/start"))}, deps)
    assert deps.users.rows[REQUESTER]["requested_at"] == NOW.isoformat()
    assert len(deps.telegram.to(ADMIN)) == 2  # the original request and the new one


def test_too_many_pending_refuses_new_requests(env, deps):
    for i in range(enrollment.MAX_PENDING):
        deps.users.create_pending(telegram_user_id=f"9{i}", chat_id=f"9{i}", first_name="x")
    run(deps, msg("/start", lang="en"))
    assert REQUESTER not in deps.users.rows
    assert "too many" in deps.telegram.to(REQUESTER)[0]["text"].lower()


def test_pending_user_can_withdraw_with_cancel(env, deps):
    run(deps, msg("/start"))
    run(deps, msg("/cancel"))
    assert REQUESTER not in deps.users.rows


# --- admin decisions -------------------------------------------------------------------

def test_approve_button_approves_and_notifies_user(env, deps):
    run(deps, msg("/start"))
    run(deps, callback(f"approve:{REQUESTER}"))
    assert deps.users.rows[REQUESTER]["status"] == "approved"
    assert deps.telegram.to(REQUESTER)[-1]["text"].startswith("✅")
    assert deps.telegram.answers


def test_only_admin_can_press_buttons(env, deps):
    run(deps, msg("/start"))
    run(deps, callback(f"approve:{REQUESTER}", from_id=REQUESTER))
    assert deps.users.rows[REQUESTER]["status"] == "pending"


def test_approving_twice_does_not_notify_again(env, deps):
    run(deps, msg("/start"))
    run(deps, callback(f"approve:{REQUESTER}"))
    before = len(deps.telegram.to(REQUESTER))
    run(deps, callback(f"approve:{REQUESTER}"))
    assert len(deps.telegram.to(REQUESTER)) == before


def test_deny_cancels_schedules_and_deletes_credentials(env, deps):
    run(deps, msg("/start"))
    deps.users.link_credentials(telegram_user_id=REQUESTER, chat_id=REQUESTER, email="a@b.c",
                                password="pw", club_id="207", priority=False)
    deps.users.set_status(REQUESTER, "approved")
    deps.bookings.rows.append({"telegram_user_id": REQUESTER, "booking_id": "b1",
                               "status": "scheduled", "schedule_name": "gym-777-x"})
    run(deps, callback(f"deny:{REQUESTER}"))

    assert deps.sched.deleted == ["gym-777-x"]
    assert deps.bookings.rows[0]["status"] == "cancelled"
    assert REQUESTER not in deps.users.secrets
    assert deps.users.rows[REQUESTER]["status"] == "denied"
    assert "email" not in deps.users.rows[REQUESTER]


def test_denied_user_is_refused(env, deps):
    run(deps, msg("/start", lang="en"))
    run(deps, callback(f"deny:{REQUESTER}"))
    run(deps, msg("/login a@b.c pw", lang="en"))
    assert "not authorized" in deps.telegram.to(REQUESTER)[-1]["text"].lower()
    deps.gym.validate.assert_not_called()


def test_remove_deletes_everything(env, deps):
    run(deps, msg("/start"))
    deps.users.link_credentials(telegram_user_id=REQUESTER, chat_id=REQUESTER, email="a@b.c",
                                password="pw", club_id="207", priority=False)
    run(deps, msg(f"/remove {REQUESTER}", user_id=ADMIN))
    assert REQUESTER not in deps.users.rows and REQUESTER not in deps.users.secrets


def test_pending_lists_requests_with_buttons(env, deps):
    run(deps, msg("/start"))
    run(deps, msg("/pending", user_id=ADMIN))
    out = deps.telegram.to(ADMIN)[-1]
    assert "Mario Rossi" in out["text"]
    assert out["markup"]["inline_keyboard"][0][0]["callback_data"] == f"approve:{REQUESTER}"


def test_decisions_are_audited(env, deps, caplog):
    run(deps, msg("/start"))
    with caplog.at_level("INFO"):
        run(deps, callback(f"approve:{REQUESTER}"))
    assert any("AUDIT" in r.message and "approve" in r.message and REQUESTER in r.message
               for r in caplog.records)


# --- guided login -----------------------------------------------------------------------

def test_guided_login_asks_email_then_password_and_links(env, deps):
    run(deps, msg("/start"))
    run(deps, callback(f"approve:{REQUESTER}"))
    run(deps, msg("/login"))
    assert "email" in deps.telegram.to(REQUESTER)[-1]["text"].lower()
    run(deps, msg("a@b.c"))
    assert deps.users.rows[REQUESTER]["login_step"] == "password"
    run(deps, msg("Pw123456", message_id=77))
    assert 77 in deps.telegram.deleted          # the password message is deleted
    assert deps.users.rows[REQUESTER].get("email") == "a@b.c"
    assert "login_step" not in deps.users.rows[REQUESTER]


def test_guided_login_rejects_bad_email(env, deps):
    run(deps, msg("/start", lang="en"))
    run(deps, callback(f"approve:{REQUESTER}"))
    run(deps, msg("/login", lang="en"))
    run(deps, msg("not-an-email", lang="en"))
    assert deps.users.rows[REQUESTER]["login_step"] == "email"
    assert "doesn't look like an email" in deps.telegram.to(REQUESTER)[-1]["text"]


def test_stop_aborts_guided_login(env, deps):
    run(deps, msg("/start"))
    run(deps, callback(f"approve:{REQUESTER}"))
    run(deps, msg("/login"))
    run(deps, msg("/stop"))
    assert "login_step" not in deps.users.rows[REQUESTER]


def test_free_text_during_login_is_not_sent_to_assistant(env, deps):
    run(deps, msg("/start"))
    run(deps, callback(f"approve:{REQUESTER}"))
    run(deps, msg("/login"))
    with mock.patch.object(handler, "enqueue_assistant") as enq:
        run(deps, msg("a@b.c"))
    enq.assert_not_called()


# --- booker safety --------------------------------------------------------------------

def test_booker_skips_when_user_no_longer_approved():
    import booker_handler

    deps = mock.Mock(spec=booker_handler.Deps)
    deps.telegram = mock.Mock()
    deps.bookings = mock.Mock()
    deps.bookings.get_booking.return_value = {
        "booking_id": "b1", "status": "scheduled", "chat_id": "777", "name": "Pilates",
        "date": "2026-11-05", "time": "18:15", "class_id": "c", "session_id": "s",
        "window_minutes": 2880}
    deps.users = mock.Mock()
    deps.users.get_user.return_value = {"status": "denied"}
    deps.secrets = mock.Mock()
    result = booker_handler.process({"telegram_user_id": "777", "booking_id": "b1"}, deps)
    assert result["status"] == "failed"
    deps.gym = mock.Mock()
    deps.gym.login.assert_not_called() if hasattr(deps.gym, "login") else None
    deps.secrets.get_user_credentials.assert_not_called()
