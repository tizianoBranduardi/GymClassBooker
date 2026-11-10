"""Operator CLI: config loading, secrets, webhook and teardown (AWS/Telegram mocked)."""

import json
from unittest import mock

import pytest
from botocore.exceptions import ClientError

import manage

BOT_TOKEN = "123456789:" + "A" * 35

VALID_ENV = {
    "GYM_API_KEY": "app-key",
    "GYM_API_BASE_URL": "https://api.example.test/v1",
    "GYM_APP_ID": "app-123",
    "ADMIN_USER_ID": "111111111",
    "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
}


def _no_ask(field):  # any prompt in a "complete env" test is a bug
    raise AssertionError(f"unexpected prompt for {field.env}")


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


# ---- config ---------------------------------------------------------------

def test_load_config_from_complete_env():
    cfg = manage.load_config(VALID_ENV, ask=_no_ask, discover_admin=lambda t: None)
    assert cfg.gym_api_base_url == "https://api.example.test/v1"
    assert cfg.admin_user_id == "111111111"
    assert cfg.model_id == manage.DEFAULT_MODEL_ID
    assert cfg.alarm_email == ""


def test_load_config_prompts_for_missing_required_values():
    env = {k: v for k, v in VALID_ENV.items() if k != "GYM_APP_ID"}
    asked = []

    def ask(field):
        asked.append(field.env)
        return "typed-app-id"

    cfg = manage.load_config(env, ask=ask, discover_admin=lambda t: None)
    assert asked == ["GYM_APP_ID"]
    assert cfg.gym_app_id == "typed-app-id"


def test_admin_id_is_discovered_from_telegram_when_missing():
    env = {k: v for k, v in VALID_ENV.items() if k != "ADMIN_USER_ID"}
    seen = []

    def discover(token):
        seen.append(token)
        return "222222222"

    cfg = manage.load_config(env, ask=_no_ask, discover_admin=discover)
    assert cfg.admin_user_id == "222222222"
    assert seen == [BOT_TOKEN]


def test_admin_id_falls_back_to_prompt_when_discovery_finds_nothing():
    env = {k: v for k, v in VALID_ENV.items() if k != "ADMIN_USER_ID"}
    cfg = manage.load_config(env, ask=lambda f: "333333333", discover_admin=lambda t: None)
    assert cfg.admin_user_id == "333333333"


@pytest.mark.parametrize("key,value", [
    ("GYM_API_BASE_URL", "http://api.example.test"),
    ("GYM_API_BASE_URL", "https://api.example.test/with space"),
    ("ADMIN_USER_ID", "not-a-number"),
    ("TELEGRAM_BOT_TOKEN", "nope"),
    ("GYM_API_KEY", "has space"),
    ("ALARM_EMAIL", "not-an-email"),
])
def test_load_config_rejects_invalid_values(key, value):
    env = {**VALID_ENV, key: value}
    with pytest.raises(manage.ConfigError) as exc:
        manage.load_config(env, ask=_no_ask, discover_admin=lambda t: None)
    assert key in str(exc.value)


def test_config_error_never_echoes_secret_values():
    env = {**VALID_ENV, "TELEGRAM_BOT_TOKEN": "supersecretvalue"}
    with pytest.raises(manage.ConfigError) as exc:
        manage.load_config(env, ask=_no_ask, discover_admin=lambda t: None)
    assert "supersecretvalue" not in str(exc.value)


def test_parameter_overrides_omit_empty_alarm_email_and_telegram_token():
    cfg = manage.load_config(VALID_ENV, ask=_no_ask, discover_admin=lambda t: None)
    overrides = manage.parameter_overrides(cfg)
    assert "GymApiKey=app-key" in overrides
    assert "AdminUserId=111111111" in overrides
    assert not any(o.startswith("AlarmEmail=") for o in overrides)
    assert not any(BOT_TOKEN in o for o in overrides)


def test_parameter_overrides_include_alarm_email_when_set():
    cfg = manage.load_config({**VALID_ENV, "ALARM_EMAIL": "me@example.test"},
                             ask=_no_ask, discover_admin=lambda t: None)
    assert "AlarmEmail=me@example.test" in manage.parameter_overrides(cfg)


def test_read_env_file_parses_comments_quotes_and_blanks(tmp_path):
    f = tmp_path / ".env.deploy"
    f.write_text('# comment\n\nGYM_APP_ID="abc"\nADMIN_USER_ID=42\nBAD LINE\n')
    assert manage.read_env_file(f) == {"GYM_APP_ID": "abc", "ADMIN_USER_ID": "42"}


def test_read_env_file_missing_returns_empty(tmp_path):
    assert manage.read_env_file(tmp_path / "nope") == {}


# ---- telegram -------------------------------------------------------------

def _json_response(payload):
    r = mock.Mock()
    r.json.return_value = payload
    return r


def test_discover_admin_id_picks_latest_human_sender():
    updates = {"ok": True, "result": [
        {"message": {"from": {"id": 1, "is_bot": False}}},
        {"message": {"from": {"id": 2, "is_bot": True}}},
        {"message": {"from": {"id": 3, "is_bot": False}}},
    ]}
    get = mock.Mock(return_value=_json_response(updates))
    assert manage.discover_admin_id(BOT_TOKEN, http_get=get) == "3"


def test_discover_admin_id_none_when_no_messages_or_error():
    assert manage.discover_admin_id(
        BOT_TOKEN, http_get=mock.Mock(return_value=_json_response({"ok": True, "result": []}))) is None
    assert manage.discover_admin_id(
        BOT_TOKEN, http_get=mock.Mock(return_value=_json_response({"ok": False}))) is None


def test_discover_admin_id_swallows_network_errors():
    import requests
    get = mock.Mock(side_effect=requests.ConnectionError())
    assert manage.discover_admin_id(BOT_TOKEN, http_get=get) is None


# ---- secrets / webhook ----------------------------------------------------

def test_ensure_secret_creates_new_secret():
    sm = mock.Mock()
    manage.ensure_secret(sm, "name", "value")
    sm.create_secret.assert_called_once_with(Name="name", SecretString="value")
    sm.put_secret_value.assert_not_called()


def test_ensure_secret_updates_existing_secret():
    sm = mock.Mock()
    sm.create_secret.side_effect = _client_error("ResourceExistsException")
    manage.ensure_secret(sm, "name", "value")
    sm.put_secret_value.assert_called_once_with(SecretId="name", SecretString="value")


def test_ensure_secret_propagates_other_errors():
    sm = mock.Mock()
    sm.create_secret.side_effect = _client_error("AccessDeniedException")
    with pytest.raises(ClientError):
        manage.ensure_secret(sm, "name", "value")


def _session(sm=None, cfn=None, scheduler=None, bedrock=None):
    clients = {"secretsmanager": sm or mock.Mock(), "cloudformation": cfn or mock.Mock(),
               "scheduler": scheduler or mock.Mock(), "bedrock-runtime": bedrock or mock.Mock(),
               "sts": mock.Mock()}
    session = mock.Mock()
    session.client.side_effect = lambda name: clients[name]
    return session


def test_register_webhook_stores_secret_and_registers_url():
    sm, cfn = mock.Mock(), mock.Mock()
    cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": [
        {"OutputKey": "WebhookUrl", "OutputValue": "https://fn.example.test/"}]}]}
    sm.get_secret_value.return_value = {"SecretString": BOT_TOKEN}
    post = mock.Mock(return_value=_json_response({"ok": True}))

    manage.register_webhook(_session(sm=sm, cfn=cfn), "stack", http_post=post)

    stored = sm.create_secret.call_args.kwargs
    assert stored["Name"] == manage.WEBHOOK_SECRET_NAME
    body = post.call_args.kwargs["json"]
    assert body["url"] == "https://fn.example.test/"
    assert body["secret_token"] == stored["SecretString"]
    assert post.call_args.args[0] == f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"


def test_register_webhook_reads_json_wrapped_bot_token():
    sm, cfn = mock.Mock(), mock.Mock()
    cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": [
        {"OutputKey": "WebhookUrl", "OutputValue": "https://fn.example.test/"}]}]}
    sm.get_secret_value.return_value = {"SecretString": json.dumps({"token": BOT_TOKEN})}
    post = mock.Mock(return_value=_json_response({"ok": True}))
    manage.register_webhook(_session(sm=sm, cfn=cfn), "stack", http_post=post)
    assert BOT_TOKEN in post.call_args.args[0]


def test_register_webhook_raises_when_telegram_refuses():
    sm, cfn = mock.Mock(), mock.Mock()
    cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": [
        {"OutputKey": "WebhookUrl", "OutputValue": "https://fn.example.test/"}]}]}
    sm.get_secret_value.return_value = {"SecretString": BOT_TOKEN}
    post = mock.Mock(return_value=_json_response({"ok": False, "description": "bad url"}))
    with pytest.raises(manage.OperationError, match="bad url"):
        manage.register_webhook(_session(sm=sm, cfn=cfn), "stack", http_post=post)


# ---- model check ----------------------------------------------------------

def test_check_model_ok():
    assert manage.check_model(mock.Mock(), "model-id") is None


def test_check_model_reports_access_problem():
    br = mock.Mock()
    br.converse.side_effect = _client_error("AccessDeniedException")
    msg = manage.check_model(br, "model-id")
    assert "model-id" in msg and "AccessDeniedException" in msg


# ---- destroy --------------------------------------------------------------

def _destroy_session():
    sm, cfn, sch = mock.Mock(), mock.Mock(), mock.Mock()
    sm.get_paginator.return_value.paginate.return_value = [
        {"SecretList": [{"Name": "gym-booker/user/1"}, {"Name": "gym-booker/user/2"}]}]
    sch.get_paginator.return_value.paginate.return_value = [
        {"Schedules": [{"Name": "gym-1-2026-11-10-1800"}]}]
    sm.get_secret_value.return_value = {"SecretString": BOT_TOKEN}
    return _session(sm=sm, cfn=cfn, scheduler=sch), sm, cfn, sch


def test_destroy_requires_matching_confirmation():
    session, sm, cfn, _ = _destroy_session()
    with pytest.raises(manage.OperationError, match="confirm"):
        manage.destroy(session, "stack", confirm="wrong", http_post=mock.Mock())
    cfn.delete_stack.assert_not_called()
    sm.delete_secret.assert_not_called()


def test_destroy_removes_stack_schedules_and_secrets():
    session, sm, cfn, sch = _destroy_session()
    post = mock.Mock(return_value=_json_response({"ok": True}))

    summary = manage.destroy(session, "stack", confirm="stack", http_post=post)

    cfn.delete_stack.assert_called_once_with(StackName="stack")
    sch.delete_schedule.assert_called_once_with(Name="gym-1-2026-11-10-1800")
    deleted = {c.kwargs["SecretId"] for c in sm.delete_secret.call_args_list}
    assert deleted == {"gym-booker/user/1", "gym-booker/user/2",
                       manage.WEBHOOK_SECRET_NAME, manage.TELEGRAM_SECRET_NAME}
    assert all(c.kwargs["ForceDeleteWithoutRecovery"] for c in sm.delete_secret.call_args_list)
    assert post.call_args.args[0].endswith("/deleteWebhook")
    assert summary == {"schedules": 1, "user_secrets": 2}


def test_destroy_tolerates_missing_secrets():
    session, sm, _, _ = _destroy_session()
    sm.delete_secret.side_effect = _client_error("ResourceNotFoundException")
    manage.destroy(session, "stack", confirm="stack",
                   http_post=mock.Mock(return_value=_json_response({"ok": True})))


# ---- command handlers -----------------------------------------------------

def _args(**kw):
    base = {"profile": None, "region": "eu-central-1", "stack": "stack", "model": None, "yes": False}
    return mock.Mock(**{**base, **kw})


def test_main_reports_config_errors_without_traceback(capsys):
    with mock.patch.dict(manage.COMMANDS, {"status": mock.Mock(side_effect=manage.ConfigError("X: bad"))}):
        assert manage.main(["status"]) == 2
    assert "error: X: bad" in capsys.readouterr().err


def test_main_reports_failed_subprocess(capsys):
    import subprocess
    err = subprocess.CalledProcessError(3, ["sam", "build"])
    with mock.patch.dict(manage.COMMANDS, {"deploy": mock.Mock(side_effect=err)}):
        assert manage.main(["deploy"]) == 3
    assert "sam build" in capsys.readouterr().err


def test_parser_defaults_and_destroy_flag():
    args = manage.build_parser().parse_args(["--stack", "s", "destroy", "--yes"])
    assert args.stack == "s" and args.yes is True and args.command == "destroy"


def test_cmd_doctor_fails_without_sam(monkeypatch):
    monkeypatch.setattr(manage.shutil, "which", lambda name: None)
    session = _session()
    session.client("sts").get_caller_identity.return_value = {"Account": "000000000000"}
    session.region_name = "eu-central-1"
    monkeypatch.setattr(manage, "_session", lambda args: session)
    assert manage.cmd_doctor(_args()) == 1


def test_cmd_doctor_passes_when_everything_works(monkeypatch):
    monkeypatch.setattr(manage.shutil, "which", lambda name: "/usr/bin/sam")
    session = _session()
    session.client("sts").get_caller_identity.return_value = {"Account": "000000000000"}
    session.region_name = "eu-central-1"
    monkeypatch.setattr(manage, "_session", lambda args: session)
    assert manage.cmd_doctor(_args()) == 0


def test_cmd_doctor_fails_without_credentials(monkeypatch):
    monkeypatch.setattr(manage.shutil, "which", lambda name: "/usr/bin/sam")
    session = _session()
    session.client("sts").get_caller_identity.side_effect = _client_error("ExpiredToken")
    session.region_name = "eu-central-1"
    monkeypatch.setattr(manage, "_session", lambda args: session)
    assert manage.cmd_doctor(_args()) == 1


def test_cmd_status_missing_stack(monkeypatch):
    session = _session()
    session.client("cloudformation").describe_stacks.side_effect = _client_error("ValidationError")
    monkeypatch.setattr(manage, "_session", lambda args: session)
    assert manage.cmd_status(_args()) == 1


def test_cmd_status_prints_webhook_health(monkeypatch, capsys):
    cfn, sm = mock.Mock(), mock.Mock()
    cfn.describe_stacks.return_value = {"Stacks": [{"StackStatus": "CREATE_COMPLETE"}]}
    sm.get_secret_value.return_value = {"SecretString": BOT_TOKEN}
    monkeypatch.setattr(manage, "_session", lambda args: _session(sm=sm, cfn=cfn))
    monkeypatch.setattr(manage.requests, "get", mock.Mock(return_value=_json_response(
        {"result": {"url": "https://x", "pending_update_count": 0}})))
    assert manage.cmd_status(_args()) == 0
    out = capsys.readouterr().out
    assert "CREATE_COMPLETE" in out and "webhook url set: True" in out


def test_cmd_destroy_with_yes_skips_prompt(monkeypatch):
    session, _, cfn, _ = _destroy_session()
    monkeypatch.setattr(manage, "_session", lambda args: session)
    monkeypatch.setattr(manage.requests, "post", mock.Mock(return_value=_json_response({"ok": True})))
    monkeypatch.setattr("builtins.input", lambda *_: (_ for _ in ()).throw(AssertionError("prompted")))
    assert manage.cmd_destroy(_args(yes=True)) == 0
    cfn.delete_stack.assert_called_once()


def test_cmd_webhook_registers(monkeypatch, capsys):
    monkeypatch.setattr(manage, "_session", lambda args: _session())
    monkeypatch.setattr(manage, "register_webhook", lambda session, stack: "https://fn.example.test/")
    assert manage.cmd_webhook(_args()) == 0
    assert "https://fn.example.test/" in capsys.readouterr().out


def test_cmd_deploy_runs_build_deploy_and_webhook(monkeypatch):
    env = {**VALID_ENV, "ALARM_EMAIL": ""}
    monkeypatch.setattr(manage, "read_env_file", lambda path: env)
    monkeypatch.setattr(manage, "_session", lambda args: _session())
    monkeypatch.setattr(manage, "register_webhook", lambda session, stack: "https://fn.example.test/")
    run = mock.Mock()
    monkeypatch.setattr(manage.subprocess, "run", run)
    session = _session()
    session.region_name = "eu-central-1"
    monkeypatch.setattr(manage, "_session", lambda args: session)

    assert manage.cmd_deploy(_args(profile="p")) == 0

    cmds = [c.args[0] for c in run.call_args_list]
    assert cmds[0] == ["sam", "build"]
    assert cmds[1][:2] == ["sam", "deploy"] and "--profile" in cmds[1]
    assert "GymApiKey=app-key" in cmds[1]


def test_cmd_deploy_aborts_when_model_unreachable(monkeypatch):
    monkeypatch.setattr(manage, "read_env_file", lambda path: dict(VALID_ENV))
    br = mock.Mock()
    br.converse.side_effect = _client_error("AccessDeniedException")
    monkeypatch.setattr(manage, "_session", lambda args: _session(bedrock=br))
    run = mock.Mock()
    monkeypatch.setattr(manage.subprocess, "run", run)
    with pytest.raises(manage.OperationError, match="cannot call model"):
        manage.cmd_deploy(_args())
    run.assert_not_called()
