import json
from unittest import mock

import pytest

import assistant
import handler

USER = "777000111"
LINKED = {"status": "approved", "email": "a@b.c", "club_id": "207",
          "priority": False, "chat_id": USER}

OPTIONS = [
    {"idx": 1, "date": "2026-10-30", "time": "18:15", "name": "Reformer Pilates", "studio": "Main",
     "booked": 5, "capacity": 20, "class_id": "c1", "session_id": "s1"},
    {"idx": 2, "date": "2026-10-30", "time": "19:00", "name": "Spin", "studio": "Cycle",
     "booked": 19, "capacity": 20, "class_id": "c2", "session_id": "s2"},
]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "eu.anthropic.claude-haiku-5-5")
    monkeypatch.setenv("DEFAULT_CLUB_ID", "207")
    return monkeypatch


@pytest.fixture
def deps():
    d = mock.Mock(spec=handler.Deps)
    for name in ("telegram", "gym", "users", "bookings", "sched"):
        setattr(d, name, mock.Mock())
    d.users.get_conv.return_value = []
    return d


class FakeBedrock:
    """Scripted converse() responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def converse(self, **kwargs):
        import copy
        self.calls.append(copy.deepcopy(kwargs))  # snapshot: the real SDK serializes at call time
        return self.responses.pop(0)


def text_response(text):
    return {"stopReason": "end_turn",
            "output": {"message": {"role": "assistant", "content": [{"text": text}]}}}


def tool_response(name, args, tool_id="t1"):
    return {"stopReason": "tool_use",
            "output": {"message": {"role": "assistant",
                                   "content": [{"toolUse": {"toolUseId": tool_id,
                                                            "name": name, "input": args}}]}}}


def test_plain_question_gets_reply_and_history_saved(env, deps):
    bedrock = FakeBedrock([text_response("Which day and time do you prefer?")])
    assistant.handle(deps, int(USER), USER, LINKED, "book me a pilates class", bedrock=bedrock)

    deps.telegram.send_message.assert_called_once()
    assert "Which day" in deps.telegram.send_message.call_args.args[1]
    saved = deps.users.save_conv.call_args.args[1]
    assert saved[-2]["content"][0]["text"] == "book me a pilates class"
    assert saved[-1]["role"] == "assistant"


def test_system_prompt_contains_today_and_no_assumption_rule(env, deps):
    bedrock = FakeBedrock([text_response("ok")])
    with mock.patch.object(assistant, "_now_rome") as now:
        import datetime, zoneinfo
        now.return_value = datetime.datetime(2026, 10, 26, 15, 0,
                                             tzinfo=zoneinfo.ZoneInfo("Europe/Rome"))
        assistant.handle(deps, int(USER), USER, LINKED, "hi", bedrock=bedrock)
    system = bedrock.calls[0]["system"][0]["text"]
    assert "2026-10-26" in system and "Monday" in system
    assert "ask" in system.lower() and "assum" in system.lower()


def test_search_tool_executes_and_feeds_result_back(env, deps):
    deps.gym.search_options.return_value = list(OPTIONS)
    bedrock = FakeBedrock([
        tool_response("search_classes", {"date": "2026-10-30", "time": "18:00"}),
        text_response("Found 2 classes: 1. Reformer Pilates ..."),
    ])
    assistant.handle(deps, int(USER), USER, LINKED, "classes tomorrow 18", bedrock=bedrock)

    deps.gym.search_options.assert_called_once_with(
        telegram_user_id=USER, club_id="207", target_date="2026-10-30", target_time="18:00")
    deps.users.save_search.assert_called_once()
    # second bedrock call carries the tool result
    tool_result_msg = bedrock.calls[1]["messages"][-1]
    assert tool_result_msg["role"] == "user"
    assert "toolResult" in tool_result_msg["content"][0]
    assert "Found 2 classes" in deps.telegram.send_message.call_args.args[1]


def test_search_tool_name_filter(env, deps):
    deps.gym.search_options.return_value = list(OPTIONS)
    bedrock = FakeBedrock([
        tool_response("search_classes", {"date": "2026-10-30", "time": "18:00",
                                         "name_filter": "pilates"}),
        text_response("1 class"),
    ])
    assistant.handle(deps, int(USER), USER, LINKED, "pilates tomorrow 18", bedrock=bedrock)

    saved = deps.users.save_search.call_args.args[1]
    assert len(saved) == 1 and saved[0]["name"] == "Reformer Pilates" and saved[0]["idx"] == 1
    result_text = bedrock.calls[1]["messages"][-1]["content"][0]["toolResult"]["content"][0]["text"]
    assert "Spin" not in result_text


def test_schedule_tool_books_via_pick(env, deps):
    deps.users.get_search.return_value = list(OPTIONS)
    deps.sched.schedule_or_book_now.return_value = {"mode": "scheduled",
                                                    "fire_at": "2026-10-28 18:12",
                                                    "schedule_name": "gym-x"}
    bedrock = FakeBedrock([
        tool_response("schedule_booking", {"number": 1}),
        text_response("Scheduled!"),
    ])
    assistant.handle(deps, int(USER), USER, LINKED, "yes book number 1", bedrock=bedrock)

    deps.bookings.create_booking.assert_called_once()
    result_text = bedrock.calls[1]["messages"][-1]["content"][0]["toolResult"]["content"][0]["text"]
    assert "scheduled" in result_text.lower() and "2026-10-28 18:12" in result_text


def test_history_is_trimmed(env, deps):
    deps.users.get_conv.return_value = [
        {"role": "user", "content": [{"text": f"m{i}"}]} for i in range(10)
    ]
    bedrock = FakeBedrock([text_response("ok")])
    assistant.handle(deps, int(USER), USER, LINKED, "hello", bedrock=bedrock)
    saved = deps.users.save_conv.call_args.args[1]
    assert len(saved) <= assistant.MAX_HISTORY


def test_tool_loop_is_bounded(env, deps):
    deps.gym.search_options.return_value = []
    bedrock = FakeBedrock([tool_response("search_classes",
                                         {"date": "2026-10-30", "time": "18:00"},
                                         tool_id=f"t{i}")
                           for i in range(assistant.MAX_TOOL_ROUNDS)]
                          + [text_response("done")])
    assistant.handle(deps, int(USER), USER, LINKED, "loop", bedrock=bedrock)
    assert len(bedrock.calls) <= assistant.MAX_TOOL_ROUNDS + 1
    deps.telegram.send_message.assert_called_once()


def test_reply_is_sent_as_html(env, deps):
    bedrock = FakeBedrock([text_response("<b>Pilates</b> at <code>18:15</code> 🔥")])
    assistant.handle(deps, int(USER), USER, LINKED, "hi", bedrock=bedrock)
    assert deps.telegram.send_message.call_args.kwargs.get("parse_mode") == "HTML"


def test_html_rejection_falls_back_to_plain_text(env, deps):
    deps.telegram.send_message.side_effect = [Exception("can't parse entities"), {"ok": True}]
    bedrock = FakeBedrock([text_response("<b>Pilates</b> at 18:15 🔥")])
    assistant.handle(deps, int(USER), USER, LINKED, "hi", bedrock=bedrock)
    assert deps.telegram.send_message.call_count == 2
    fallback = deps.telegram.send_message.call_args_list[1]
    assert "parse_mode" not in fallback.kwargs
    assert "<b>" not in fallback.args[1] and "Pilates" in fallback.args[1]


def test_markdown_is_converted_to_telegram_html(env, deps):
    md = "Ecco:\n- **Pilates** alle 18\n- **Spin** alle 19"
    bedrock = FakeBedrock([text_response(md)])
    assistant.handle(deps, int(USER), USER, LINKED, "hi", bedrock=bedrock)
    sent = deps.telegram.send_message.call_args.args[1]
    assert "**" not in sent and "<b>Pilates</b>" in sent and "• " in sent


def test_reply_length_is_capped(env, deps):
    bedrock = FakeBedrock([text_response("ok")])
    assistant.handle(deps, int(USER), USER, LINKED, "hi", bedrock=bedrock)
    assert bedrock.calls[0]["inferenceConfig"]["maxTokens"] <= 300


def test_system_prompt_forces_user_language(env, deps):
    bedrock = FakeBedrock([text_response("ok")])
    assistant.handle(deps, int(USER), USER, LINKED, "Cosa puoi fare?", bedrock=bedrock)
    system = bedrock.calls[0]["system"][0]["text"]
    assert "Italian" in system and "same language" in system


def test_system_prompt_asks_for_brief_playful_html(env, deps):
    bedrock = FakeBedrock([text_response("ok")])
    assistant.handle(deps, int(USER), USER, LINKED, "hi", bedrock=bedrock)
    system = bedrock.calls[0]["system"][0]["text"]
    assert "HTML" in system and "emoji" in system.lower() and "brief" in system.lower()


def test_guardrail_attached_to_every_model_call(env, deps):
    env.setenv("BEDROCK_GUARDRAIL_ID", "gr123")
    env.setenv("BEDROCK_GUARDRAIL_VERSION", "4")
    deps.gym.search_options.return_value = list(OPTIONS)
    bedrock = FakeBedrock([
        tool_response("search_classes", {"date": "2026-10-30", "time": "18:00"}),
        text_response("ok"),
    ])
    assistant.handle(deps, int(USER), USER, LINKED, "pilates tomorrow 18", bedrock=bedrock)
    for call in bedrock.calls:
        assert call["guardrailConfig"] == {"guardrailIdentifier": "gr123",
                                           "guardrailVersion": "4", "trace": "disabled"}


def test_no_guardrail_config_when_unset(env, deps, monkeypatch):
    monkeypatch.delenv("BEDROCK_GUARDRAIL_ID", raising=False)
    bedrock = FakeBedrock([text_response("ok")])
    assistant.handle(deps, int(USER), USER, LINKED, "hi", bedrock=bedrock)
    assert "guardrailConfig" not in bedrock.calls[0]


def test_guardrail_intervention_reply_is_forwarded(env, deps):
    env.setenv("BEDROCK_GUARDRAIL_ID", "gr123")
    env.setenv("BEDROCK_GUARDRAIL_VERSION", "4")
    blocked = {"stopReason": "guardrail_intervened",
               "output": {"message": {"role": "assistant", "content": [
                   {"text": "I can only help with searching and booking gym classes."}]}}}
    bedrock = FakeBedrock([blocked])
    assistant.handle(deps, int(USER), USER, LINKED, "what is the capital of France", bedrock=bedrock)
    sent = deps.telegram.send_message.call_args.args[1]
    assert "only help with searching and booking" in sent


def test_bedrock_failure_sends_apology(env, deps):
    class Boom:
        def converse(self, **kw):
            raise RuntimeError("bedrock down")
    assistant.handle(deps, int(USER), USER, LINKED, "hi", bedrock=Boom())
    msg = deps.telegram.send_message.call_args.args[1]
    assert "try again" in msg.lower() or "sorry" in msg.lower()


# --- webhook routing ------------------------------------------------------------

def test_non_command_text_from_linked_user_is_enqueued(env, deps, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_ID", "111111111")
    monkeypatch.setenv("WEBHOOK_SECRET_ID", "x")
    deps.users.get_user.return_value = LINKED
    event = {"headers": {"x-telegram-bot-api-secret-token": "s3cret"},
             "body": json.dumps({"message": {"message_id": 1,
                                             "from": {"id": int(USER), "first_name": "T"},
                                             "chat": {"id": int(USER)},
                                             "text": "book me pilates tomorrow"}})}
    with mock.patch.object(handler, "get_webhook_secret", return_value="s3cret"), \
         mock.patch.object(handler, "enqueue_assistant") as enq:
        handler.process_event(event, deps)
    enq.assert_called_once()
    deps.telegram.send_message.assert_not_called()


def test_assistant_job_event_runs_assistant(env, deps, monkeypatch):
    event = {"assistant_job": {"chat_id": int(USER), "user_id": USER,
                               "text": "pilates tomorrow"}}
    deps.users.get_user.return_value = LINKED
    with mock.patch.object(handler.assistant, "handle") as h:
        handler.process_assistant_job(event["assistant_job"], deps)
    h.assert_called_once()
