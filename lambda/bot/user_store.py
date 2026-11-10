"""Telegram-user ↔ gym-account association, with enrollment status.

Record lifecycle in DynamoDB (table USERS_TABLE, pk telegram_user_id):
  pending (first contact, with name/username/language)
    → approved (admin)  → /login → credentials in secret gym-booker/user/<id>
    → denied (admin)    → credentials deleted, schedules cancelled
  remove (admin)        → record and secret deleted
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Attr

from booking_store import SECRET_PREFIX  # single source of truth for the secret naming

logger = logging.getLogger(__name__)


def _table():
    return boto3.resource("dynamodb").Table(os.environ["USERS_TABLE"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_user(telegram_user_id: str) -> Optional[dict]:
    resp = _table().get_item(Key={"telegram_user_id": str(telegram_user_id)})
    return resp.get("Item")


def list_users() -> list[dict]:
    return _table().scan().get("Items", [])


def count_pending() -> int:
    resp = _table().scan(FilterExpression=Attr("status").eq("pending"), Select="COUNT")
    return int(resp.get("Count", 0))


def create_pending(*, telegram_user_id: str, chat_id: str, first_name: str,
                   last_name: str = "", username: str = "", language: str = "en") -> None:
    _table().put_item(Item={
        "telegram_user_id": str(telegram_user_id),
        "chat_id": chat_id,
        "first_name": first_name,
        "last_name": last_name,
        "username": username,
        "language": language,
        "status": "pending",
        "requested_at": _now(),
    })
    logger.info("AUDIT action=request user=%s name=%s", telegram_user_id, first_name)


def touch_request(telegram_user_id: str) -> None:
    """Restart the pending clock (used for admin reminders)."""
    _table().update_item(
        Key={"telegram_user_id": str(telegram_user_id)},
        UpdateExpression="SET requested_at = :t",
        ExpressionAttributeValues={":t": _now()},
    )


def set_status(telegram_user_id: str, status: str) -> None:
    _table().update_item(
        Key={"telegram_user_id": str(telegram_user_id)},
        UpdateExpression="SET #s = :s, decided_at = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": status, ":t": _now()},
    )


def _delete_secret(telegram_user_id: str) -> None:
    sm = boto3.client("secretsmanager")
    try:
        # Force deletion: the password must actually disappear, not sit in a recovery window.
        sm.delete_secret(SecretId=f"{SECRET_PREFIX}{telegram_user_id}", ForceDeleteWithoutRecovery=True)
    except sm.exceptions.ResourceNotFoundException:
        pass


def revoke_access(telegram_user_id: str) -> None:
    """Deny: delete the credentials and everything tied to the linked account."""
    _delete_secret(telegram_user_id)
    _table().update_item(
        Key={"telegram_user_id": str(telegram_user_id)},
        UpdateExpression=(
            "SET #s = :denied, decided_at = :t "
            "REMOVE email, club_id, priority, secret_name, login_step, pending_email, conv, last_search"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":denied": "denied", ":t": _now()},
    )
    logger.info("AUDIT action=revoke user=%s", telegram_user_id)


def delete_user(telegram_user_id: str) -> None:
    """Remove: delete the record and the credentials entirely."""
    _delete_secret(telegram_user_id)
    _table().delete_item(Key={"telegram_user_id": str(telegram_user_id)})
    logger.info("AUDIT action=remove user=%s", telegram_user_id)


def set_login_step(telegram_user_id: str, step: Optional[str], email: Optional[str] = None) -> None:
    """Guided /login state: 'email' → 'password' (with pending_email), or None to clear."""
    if step is None:
        _table().update_item(
            Key={"telegram_user_id": str(telegram_user_id)},
            UpdateExpression="REMOVE login_step, pending_email",
        )
        return
    expr = "SET login_step = :s"
    values = {":s": step}
    if email is not None:
        expr += ", pending_email = :e"
        values[":e"] = email
    _table().update_item(
        Key={"telegram_user_id": str(telegram_user_id)},
        UpdateExpression=expr,
        ExpressionAttributeValues=values,
    )


def save_conv(telegram_user_id: str, messages: list[dict]) -> None:
    """Persist the assistant conversation history (text turns only)."""
    _table().update_item(
        Key={"telegram_user_id": str(telegram_user_id)},
        UpdateExpression="SET conv = :c, conv_at = :t",
        ExpressionAttributeValues={":c": json.dumps(messages), ":t": _now()},
    )


def get_conv(telegram_user_id: str) -> list[dict]:
    user = get_user(telegram_user_id)
    raw = (user or {}).get("conv")
    return json.loads(raw) if raw else []


def save_search(telegram_user_id: str, options: list[dict]) -> None:
    """Cache the latest /search results (JSON string keeps DynamoDB types simple)."""
    _table().update_item(
        Key={"telegram_user_id": str(telegram_user_id)},
        UpdateExpression="SET last_search = :o, searched_at = :t",
        ExpressionAttributeValues={":o": json.dumps(options), ":t": _now()},
    )


def get_search(telegram_user_id: str) -> Optional[list[dict]]:
    user = get_user(telegram_user_id)
    raw = (user or {}).get("last_search")
    return json.loads(raw) if raw else None


def link_credentials(*, telegram_user_id: str, chat_id: str, email: str, password: str,
                     club_id: str, priority: bool) -> None:
    """Store credentials in the per-user secret and the profile on the record."""
    secret_name = f"{SECRET_PREFIX}{telegram_user_id}"
    secret_value = json.dumps({
        "email": email,
        "password": password,
        "api_key": os.environ["GYM_API_KEY"],
        "club_id": club_id,
    })
    sm = boto3.client("secretsmanager")
    try:
        sm.create_secret(Name=secret_name, SecretString=secret_value)
    except sm.exceptions.ResourceExistsException:
        sm.put_secret_value(SecretId=secret_name, SecretString=secret_value)

    # Update (not replace) so enrollment fields survive; the admin's own record
    # may not exist yet, in which case this upsert also marks it approved.
    _table().update_item(
        Key={"telegram_user_id": str(telegram_user_id)},
        UpdateExpression=(
            "SET chat_id = :c, email = :e, club_id = :k, priority = :p, "
            "secret_name = :n, linked_at = :t, #s = if_not_exists(#s, :approved) "
            "REMOVE login_step, pending_email"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":c": chat_id, ":e": email, ":k": club_id, ":p": priority,
            ":n": secret_name, ":t": _now(), ":approved": "approved",
        },
    )
    logger.info("AUDIT action=link user=%s", telegram_user_id)
