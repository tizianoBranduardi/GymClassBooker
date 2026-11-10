#!/usr/bin/env python3
"""Operator CLI: deploy, wire up, inspect and tear down the booking stack.

    python scripts/manage.py doctor     # check tools, credentials, model access
    python scripts/manage.py deploy     # secrets + sam build/deploy + Telegram webhook
    python scripts/manage.py webhook    # (re)register the Telegram webhook
    python scripts/manage.py status     # stack status and webhook health
    python scripts/manage.py destroy    # delete everything this stack created

Configuration comes from environment variables or a git-ignored `.env.deploy`
file (see `.env.example`); anything missing is asked for interactively.
AWS credentials use the standard boto3 chain (`--profile`, `AWS_PROFILE`, ...).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets as pysecrets
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

import boto3
import requests
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "lambda" / "bot"
ENV_FILE = ROOT / ".env.deploy"

DEFAULT_STACK = "bookingscript-bot"
DEFAULT_MODEL_ID = "eu.amazon.nova-micro-v1:0"
DEFAULT_CLUB_ID = "207"
WEBHOOK_SECRET_NAME = "gym-booker/webhook-secret"
TELEGRAM_SECRET_NAME = "telegram_bot_key"
USER_SECRET_PREFIX = "gym-booker/user/"
SCHEDULE_PREFIX = "gym-"
TELEGRAM_API = "https://api.telegram.org"
HTTP_TIMEOUT = 15


class ConfigError(Exception):
    """Invalid or missing deployment configuration."""


class OperationError(Exception):
    """An operation could not be completed."""


# ---- configuration --------------------------------------------------------

def _https_url(v: str) -> str | None:
    ok = v.startswith("https://") and not re.search(r"\s", v)
    return None if ok else "must be an https:// URL without spaces"


def _digits(v: str) -> str | None:
    return None if v.isdigit() else "must be a number"


def _bot_token(v: str) -> str | None:
    return None if re.fullmatch(r"\d+:[A-Za-z0-9_-]{30,}", v) else "does not look like a Telegram bot token"


def _no_space(v: str) -> str | None:
    return None if v and not re.search(r"\s", v) else "must not contain spaces"


def _email_or_empty(v: str) -> str | None:
    return None if not v or re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v) else "must be an email address"


@dataclass(frozen=True)
class Field:
    attr: str
    env: str
    label: str
    validate: Callable[[str], str | None]
    secret: bool = False
    required: bool = True
    default: str = ""


FIELDS: tuple[Field, ...] = (
    Field("gym_api_key", "GYM_API_KEY", "Gym booking API key", _no_space, secret=True),
    Field("gym_api_base_url", "GYM_API_BASE_URL", "Gym booking API base URL", _https_url),
    Field("gym_app_id", "GYM_APP_ID", "Gym booking app id (X-App-ID)", _no_space),
    Field("bot_token", "TELEGRAM_BOT_TOKEN", "Telegram bot token (from @BotFather)", _bot_token, secret=True),
    Field("admin_user_id", "ADMIN_USER_ID", "Your Telegram user id", _digits),
    Field("model_id", "BEDROCK_MODEL_ID", "Bedrock model / inference profile id", _no_space,
          required=False, default=DEFAULT_MODEL_ID),
    Field("default_club_id", "DEFAULT_CLUB_ID", "Default club id", _digits,
          required=False, default=DEFAULT_CLUB_ID),
    Field("alarm_email", "ALARM_EMAIL", "Email for booker-error alarms (optional)", _email_or_empty,
          required=False),
)


@dataclass(frozen=True)
class DeployConfig:
    gym_api_key: str
    gym_api_base_url: str
    gym_app_id: str
    bot_token: str
    admin_user_id: str
    model_id: str
    default_club_id: str
    alarm_email: str


def _resolve(field: Field, env: Mapping[str, str], ask: Callable[[Field], str],
             pre: str | None) -> str:
    value = (pre if pre is not None else env.get(field.env, "")).strip()
    if not value and field.required:
        value = ask(field).strip()
    value = value or field.default
    error = field.validate(value) if (value or field.required) else None
    if error:
        # never echo the value: it may be a secret
        raise ConfigError(f"{field.env}: {error}")
    return value


def load_config(env: Mapping[str, str], *, ask: Callable[[Field], str],
                discover_admin: Callable[[str], str | None]) -> DeployConfig:
    """Build a validated config from `env`, asking for whatever is missing.

    The admin's Telegram id is discovered from the bot's pending messages when
    not provided, so a new operator only has to message their bot once.
    """
    values: dict[str, str] = {}
    for field in FIELDS:
        pre = None
        if field.attr == "admin_user_id" and not env.get(field.env, "").strip():
            pre = discover_admin(values["bot_token"]) or ""
        values[field.attr] = _resolve(field, env, ask, pre)
    return DeployConfig(**values)


def parameter_overrides(cfg: DeployConfig) -> list[str]:
    pairs = [
        ("GymApiKey", cfg.gym_api_key),
        ("GymApiBaseUrl", cfg.gym_api_base_url),
        ("GymAppId", cfg.gym_app_id),
        ("AdminUserId", cfg.admin_user_id),
        ("BedrockModelId", cfg.model_id),
        ("DefaultClubId", cfg.default_club_id),
        ("TelegramSecretName", TELEGRAM_SECRET_NAME),
        ("WebhookSecretName", WEBHOOK_SECRET_NAME),
    ]
    if cfg.alarm_email:
        pairs.append(("AlarmEmail", cfg.alarm_email))
    return [f"{k}={v}" for k, v in pairs]


def read_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines (comments and blank lines ignored)."""
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip("'\"")
    return out


def prompt_field(field: Field) -> str:
    text = f"{field.label}: "
    return getpass.getpass(text) if field.secret else input(text)


# ---- Telegram -------------------------------------------------------------

def discover_admin_id(bot_token: str, http_get: Callable = requests.get) -> str | None:
    """Telegram user id of whoever last messaged the bot (works before a webhook is set)."""
    try:
        data = http_get(f"{TELEGRAM_API}/bot{bot_token}/getUpdates", timeout=HTTP_TIMEOUT).json()
    except requests.RequestException:
        return None
    if not data.get("ok"):
        return None
    for update in reversed(data.get("result", [])):
        sender = (update.get("message") or {}).get("from") or {}
        if "id" in sender and not sender.get("is_bot"):
            return str(sender["id"])
    return None


def _telegram(method: str, bot_token: str, http_post: Callable, payload: dict | None = None) -> dict:
    resp = http_post(f"{TELEGRAM_API}/bot{bot_token}/{method}", json=payload or {}, timeout=HTTP_TIMEOUT)
    data = resp.json()
    if not data.get("ok"):
        raise OperationError(f"Telegram {method} failed: {data.get('description', 'unknown error')}")
    return data


# ---- AWS helpers ----------------------------------------------------------

def ensure_secret(sm, name: str, value: str) -> None:
    try:
        sm.create_secret(Name=name, SecretString=value)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceExistsException":
            raise
        sm.put_secret_value(SecretId=name, SecretString=value)


def _bot_token_from_secret(sm) -> str:
    raw = sm.get_secret_value(SecretId=TELEGRAM_SECRET_NAME)["SecretString"].strip()
    if raw.startswith("{"):
        return str(next(iter(json.loads(raw).values())))
    return raw


def _stack_output(cfn, stack: str, key: str) -> str:
    outputs = cfn.describe_stacks(StackName=stack)["Stacks"][0].get("Outputs", [])
    for o in outputs:
        if o["OutputKey"] == key:
            return o["OutputValue"]
    raise OperationError(f"stack {stack} has no output {key}; is it deployed?")


def check_model(bedrock, model_id: str) -> str | None:
    """None when the model answers, else a human-readable problem."""
    try:
        bedrock.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": "ping"}]}],
            inferenceConfig={"maxTokens": 5},
        )
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        return (f"cannot call model {model_id}: {code}. Enable access to it in the Bedrock "
                f"console for this region, or pass another BEDROCK_MODEL_ID.")
    return None


def register_webhook(session, stack: str, http_post: Callable = requests.post) -> str:
    """Rotate the webhook secret and point Telegram at the stack's Function URL."""
    sm, cfn = session.client("secretsmanager"), session.client("cloudformation")
    url = _stack_output(cfn, stack, "WebhookUrl")
    token = pysecrets.token_urlsafe(48)
    ensure_secret(sm, WEBHOOK_SECRET_NAME, token)
    _telegram("setWebhook", _bot_token_from_secret(sm), http_post,
              {"url": url, "secret_token": token, "drop_pending_updates": True})
    return url


def _paged(client, operation: str, key: str, **kwargs):
    for page in client.get_paginator(operation).paginate(**kwargs):
        yield from page[key]


def destroy(session, stack: str, *, confirm: str, http_post: Callable = requests.post) -> dict[str, int]:
    """Delete the stack and everything created outside it (schedules, secrets)."""
    if confirm != stack:
        raise OperationError(f"confirm by typing the stack name ({stack}) exactly")
    sm, cfn, sched = (session.client(n) for n in ("secretsmanager", "cloudformation", "scheduler"))

    try:
        _telegram("deleteWebhook", _bot_token_from_secret(sm), http_post)
    except (ClientError, OperationError, requests.RequestException) as exc:
        print(f"  warning: could not remove the Telegram webhook ({exc.__class__.__name__})")

    cfn.delete_stack(StackName=stack)
    cfn.get_waiter("stack_delete_complete").wait(StackName=stack)

    schedules = [s["Name"] for s in _paged(sched, "list_schedules", "Schedules", NamePrefix=SCHEDULE_PREFIX)]
    for name in schedules:
        sched.delete_schedule(Name=name)

    users = [s["Name"] for s in _paged(
        sm, "list_secrets", "SecretList",
        Filters=[{"Key": "name", "Values": [USER_SECRET_PREFIX]}])]
    for name in (*users, WEBHOOK_SECRET_NAME, TELEGRAM_SECRET_NAME):
        try:
            sm.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
    return {"schedules": len(schedules), "user_secrets": len(users)}


# ---- commands -------------------------------------------------------------

def _session(args) -> boto3.Session:
    return boto3.Session(profile_name=args.profile, region_name=args.region)


def cmd_doctor(args) -> int:
    sam = shutil.which("sam")
    print(f"{'ok  ' if sam else 'FAIL'} sam: {sam or 'not installed (https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)'}")
    ok = bool(sam)
    session = _session(args)
    try:
        ident = session.client("sts").get_caller_identity()
        print(f"ok   AWS account {ident['Account']} in {session.region_name}")
    except Exception as exc:  # no credentials, expired SSO, bad profile ...
        print(f"FAIL AWS credentials: {exc.__class__.__name__}: {exc}")
        return 1
    problem = check_model(session.client("bedrock-runtime"), args.model or DEFAULT_MODEL_ID)
    print(f"{'ok  ' if not problem else 'FAIL'} Bedrock model: {problem or 'reachable'}")
    return 0 if ok and not problem else 1


def cmd_deploy(args) -> int:
    env = {**read_env_file(ENV_FILE), **os.environ}
    cfg = load_config(env, ask=prompt_field, discover_admin=_discover_with_hint)
    session = _session(args)
    problem = check_model(session.client("bedrock-runtime"), cfg.model_id)
    if problem:
        raise OperationError(problem)

    ensure_secret(session.client("secretsmanager"), TELEGRAM_SECRET_NAME, cfg.bot_token)
    flags = ["--region", session.region_name] + (["--profile", args.profile] if args.profile else [])
    subprocess.run(["sam", "build"], cwd=TEMPLATE_DIR, check=True)
    subprocess.run(
        ["sam", "deploy", "--stack-name", args.stack, "--resolve-s3",
         "--capabilities", "CAPABILITY_IAM", "--no-confirm-changeset",
         "--no-fail-on-empty-changeset", *flags,
         "--parameter-overrides", *parameter_overrides(cfg)],
        cwd=TEMPLATE_DIR, check=True)
    url = register_webhook(session, args.stack)
    print(f"\nDeployed. Webhook registered at {url}\nOpen your bot in Telegram and send /start.")
    return 0


def _discover_with_hint(bot_token: str) -> str | None:
    input("Send any message to your bot in Telegram now, then press Enter... ")
    found = discover_admin_id(bot_token)
    print(f"  detected your Telegram user id: {found}" if found else "  could not detect it")
    return found


def cmd_webhook(args) -> int:
    print(f"Webhook registered at {register_webhook(_session(args), args.stack)}")
    return 0


def cmd_status(args) -> int:
    session = _session(args)
    cfn, sm = session.client("cloudformation"), session.client("secretsmanager")
    try:
        status = cfn.describe_stacks(StackName=args.stack)["Stacks"][0]["StackStatus"]
    except ClientError as exc:
        print(f"stack {args.stack}: {exc.response['Error']['Code']}")
        return 1
    print(f"stack {args.stack}: {status}")
    info = requests.get(f"{TELEGRAM_API}/bot{_bot_token_from_secret(sm)}/getWebhookInfo",
                        timeout=HTTP_TIMEOUT).json().get("result", {})
    print(f"webhook url set: {bool(info.get('url'))}, pending updates: {info.get('pending_update_count')}, "
          f"last error: {info.get('last_error_message') or 'none'}")
    return 0


def cmd_destroy(args) -> int:
    confirm = args.stack if args.yes else input(
        f"This deletes stack {args.stack}, all users' stored gym credentials and the bot token secret.\n"
        f"Type the stack name to confirm: ").strip()
    summary = destroy(_session(args), args.stack, confirm=confirm)
    print(f"Deleted stack, {summary['schedules']} schedule(s), {summary['user_secrets']} user secret(s).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    p.add_argument("--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
    p.add_argument("--stack", default=os.environ.get("STACK_NAME", DEFAULT_STACK))
    sub = p.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--model", default=None)
    for name in ("deploy", "webhook", "status"):
        sub.add_parser(name)
    destroy_p = sub.add_parser("destroy")
    destroy_p.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    return p


COMMANDS = {"doctor": cmd_doctor, "deploy": cmd_deploy, "webhook": cmd_webhook,
            "status": cmd_status, "destroy": cmd_destroy}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except (ConfigError, OperationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"error: `{exc.cmd[0]} {exc.cmd[1]}` failed (exit {exc.returncode})", file=sys.stderr)
        return exc.returncode or 1


if __name__ == "__main__":
    sys.exit(main())
