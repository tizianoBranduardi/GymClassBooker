"""Contract tests: every call the handlers make on their Deps must bind to the
real modules' signatures. Mocked-deps tests cannot catch a signature mismatch
(that's how a TypeError reached production once); these do."""

import inspect

import booking_store
import scheduler_client
import telegram_api
import user_store
import gym_client

# (module, function name, kwargs exactly as the handlers call them)
CALLS = [
    (telegram_api, "send_message", {"chat_id": 1, "text": "x"}),
    (telegram_api, "delete_message", {"chat_id": 1, "message_id": 2}),
    (gym_client, "validate", {"email": "a", "password": "b"}),
    (gym_client, "login", {"email": "a", "password": "b", "api_key": "k"}),
    (gym_client, "search_options", {"telegram_user_id": "1", "club_id": "207",
                                   "target_date": "2026-10-30", "target_time": "18:00"}),
    (gym_client, "book_once", {"token": "t", "api_key": "k", "club_id": "207",
                              "class_id": "c", "session_id": "s", "class_date": "2026-10-30"}),
    (user_store, "get_user", {"telegram_user_id": "1"}),
    (user_store, "create_pending", {"telegram_user_id": "1", "chat_id": "1", "first_name": "T"}),
    (user_store, "set_status", {"telegram_user_id": "1", "status": "approved"}),
    (user_store, "list_users", {}),
    (user_store, "save_search", {"telegram_user_id": "1", "options": []}),
    (user_store, "count_pending", {}),
    (user_store, "touch_request", {"telegram_user_id": "1"}),
    (user_store, "revoke_access", {"telegram_user_id": "1"}),
    (user_store, "delete_user", {"telegram_user_id": "1"}),
    (user_store, "set_login_step", {"telegram_user_id": "1", "step": "email"}),
    (telegram_api, "answer_callback_query", {"callback_id": "c", "text": "ok"}),
    (user_store, "save_conv", {"telegram_user_id": "1", "messages": []}),
    (user_store, "get_conv", {"telegram_user_id": "1"}),
    (user_store, "get_search", {"telegram_user_id": "1"}),
    (user_store, "link_credentials", {"telegram_user_id": "1", "chat_id": "1", "email": "a",
                                      "password": "b", "club_id": "207", "priority": False}),
    (booking_store, "create_booking", {"telegram_user_id": "1", "chat_id": "1",
                                       "option": {}, "window_minutes": 2880,
                                       "schedule_name": "n", "fire_at": "now"}),
    (booking_store, "get_booking", {"telegram_user_id": "1", "booking_id": "b"}),
    (booking_store, "list_bookings", {"telegram_user_id": "1"}),
    (booking_store, "set_status", {"telegram_user_id": "1", "booking_id": "b", "status": "booked"}),
    (booking_store, "delete_booking", {"telegram_user_id": "1", "booking_id": "b"}),
    (booking_store, "get_user_credentials", {"telegram_user_id": "1"}),
    (scheduler_client, "schedule_or_book_now", {"telegram_user_id": "1", "booking_id": "b",
                                                "option": {}, "window_minutes": 2880}),
    (scheduler_client, "delete_schedule", {"name": "n"}),
]


def test_handler_calls_bind_to_real_signatures():
    failures = []
    for module, name, kwargs in CALLS:
        fn = getattr(module, name, None)
        if fn is None:
            failures.append(f"{module.__name__}.{name} does not exist")
            continue
        try:
            inspect.signature(fn).bind(**kwargs)
        except TypeError as e:
            failures.append(f"{module.__name__}.{name}: {e}")
    assert not failures, "\n".join(failures)
