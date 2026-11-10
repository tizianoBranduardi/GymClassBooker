import json
from unittest import mock

import pytest

import handler

ADMIN = 111111111
STRANGER = 777000111


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_ID", str(ADMIN))
    monkeypatch.setenv("DEFAULT_CLUB_ID", "207")
    monkeypatch.setenv("WEBHOOK_SECRET_ID", "gym-booker/webhook-secret")
    return monkeypatch


@pytest.fixture
def deps():
    d = mock.Mock(spec=handler.Deps)
    d.telegram = mock.Mock()
    d.gym = mock.Mock()
    d.users = mock.Mock()
    return d


def tg_event(text, user_id=ADMIN, chat_id=None, message_id=10, secret="s3cret", first_name="T"):
    update = {"message": {"message_id": message_id,
                          "from": {"id": user_id, "first_name": first_name},
                          "chat": {"id": chat_id if chat_id is not None else user_id, "type": "private"},
                          "text": text}}
    return {"headers": {"x-telegram-bot-api-secret-token": secret},
            "body": json.dumps(update)}


def run(deps, event):
    with mock.patch.object(handler, "get_webhook_secret", return_value="s3cret"):
        return handler.process_event(event, deps)


def sent_texts(deps):
    return [c.args[1] for c in deps.telegram.send_message.call_args_list]


# --- webhook auth -------------------------------------------------------------

def test_rejects_wrong_webhook_secret(env, deps):
    with mock.patch.object(handler, "get_webhook_secret", return_value="s3cret"):
        resp = handler.process_event(tg_event("/start", secret="WRONG"), deps)
    assert resp["statusCode"] == 403
    deps.telegram.send_message.assert_not_called()


@pytest.mark.parametrize("headers", [{}, {"x-telegram-bot-api-secret-token": ""},
                                     {"x-telegram-bot-api-secret-token": "sécret-ünicode"}])
def test_rejects_missing_empty_or_non_ascii_secret(env, deps, headers):
    event = {"headers": headers, "body": "{}"}
    with mock.patch.object(handler, "get_webhook_secret", return_value="s3cret"):
        resp = handler.process_event(event, deps)
    assert resp["statusCode"] == 403
    deps.telegram.send_message.assert_not_called()


# --- enrollment ----------------------------------------------------------------

def test_denied_user_is_refused(env, deps):
    deps.users.get_user.return_value = {"status": "denied"}
    run(deps, tg_event("/start", user_id=STRANGER))
    assert "not authorized" in sent_texts(deps)[0].lower()
    deps.users.create_pending.assert_not_called()


# --- admin commands ---------------------------------------------------------------

def test_admin_approve_updates_status_and_notifies_user(env, deps):
    deps.users.get_user.return_value = {"status": "pending", "chat_id": str(STRANGER), "first_name": "Fede"}
    run(deps, tg_event(f"/approve {STRANGER}", user_id=ADMIN))
    deps.users.set_status.assert_called_once_with(str(STRANGER), "approved")
    targets = [str(c.args[0]) for c in deps.telegram.send_message.call_args_list]
    assert str(STRANGER) in targets  # the user is told
    assert str(ADMIN) in targets     # the admin gets confirmation


def test_admin_approve_unknown_id(env, deps):
    deps.users.get_user.return_value = None
    run(deps, tg_event("/approve 42", user_id=ADMIN))
    deps.users.set_status.assert_not_called()
    assert "no request" in sent_texts(deps)[0].lower()


def test_non_admin_cannot_approve(env, deps):
    deps.users.get_user.return_value = {"status": "approved"}
    run(deps, tg_event(f"/approve {STRANGER}", user_id=STRANGER))
    deps.users.set_status.assert_not_called()


def test_admin_users_lists_everyone(env, deps):
    deps.users.list_users.return_value = [
        {"telegram_user_id": str(ADMIN), "status": "approved", "email": "a@b.c", "first_name": "T"},
        {"telegram_user_id": str(STRANGER), "status": "pending", "first_name": "Fede"},
    ]
    run(deps, tg_event("/users", user_id=ADMIN))
    out = sent_texts(deps)[0]
    assert str(STRANGER) in out and "pending" in out and "a@b.c" in out


# --- /login (approved users and admin) ----------------------------------------------

def test_login_approved_user(env, deps):
    deps.users.get_user.return_value = {"status": "approved"}
    deps.gym.validate.return_value = {"ok": True, "priority": True}
    run(deps, tg_event("/login a@b.c Pw123456.", user_id=STRANGER, message_id=44))

    deps.telegram.delete_message.assert_called_once_with(STRANGER, 44)
    deps.gym.validate.assert_called_once_with("a@b.c", "Pw123456.")
    kwargs = deps.users.link_credentials.call_args.kwargs
    assert kwargs["telegram_user_id"] == str(STRANGER)
    assert kwargs["priority"] is True
    assert "PRIORITY" in sent_texts(deps)[0]


def test_admin_can_login_without_any_record(env, deps):
    deps.users.get_user.return_value = None
    deps.gym.validate.return_value = {"ok": True, "priority": False}
    run(deps, tg_event("/login a@b.c pw", user_id=ADMIN))
    deps.users.link_credentials.assert_called_once()
    deps.users.create_pending.assert_not_called()


def test_login_bad_credentials(env, deps):
    deps.users.get_user.return_value = {"status": "approved"}
    deps.gym.validate.return_value = {"ok": False}
    run(deps, tg_event("/login a@b.c wrong", user_id=STRANGER))
    deps.users.link_credentials.assert_not_called()
    assert "failed" in sent_texts(deps)[0].lower()


def test_login_never_echoes_password(env, deps):
    deps.users.get_user.return_value = {"status": "approved"}
    deps.gym.validate.return_value = {"ok": False}
    run(deps, tg_event("/login a@b.c SuperSecret99", user_id=STRANGER))
    assert all("SuperSecret99" not in t for t in sent_texts(deps))


def test_login_usage_on_wrong_args(env, deps):
    deps.users.get_user.return_value = {"status": "approved"}
    run(deps, tg_event("/login nope", user_id=STRANGER))
    deps.telegram.delete_message.assert_called_once()
    assert "usage" in sent_texts(deps)[0].lower()


# --- /status --------------------------------------------------------------------------

def test_status_linked(env, deps):
    deps.users.get_user.return_value = {"status": "approved", "email": "a@b.c",
                                        "club_id": "207", "priority": False}
    run(deps, tg_event("/status", user_id=STRANGER))
    assert "a@b.c" in sent_texts(deps)[0]


def test_status_approved_not_linked(env, deps):
    deps.users.get_user.return_value = {"status": "approved"}
    run(deps, tg_event("/status", user_id=STRANGER))
    assert "/login" in sent_texts(deps)[0]


# --- robustness -------------------------------------------------------------------------

def test_garbage_body_returns_200(env, deps):
    event = {"headers": {"x-telegram-bot-api-secret-token": "s3cret"}, "body": "{not json"}
    resp = run(deps, event)
    assert resp["statusCode"] == 200
