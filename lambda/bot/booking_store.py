"""Bookings table CRUD + per-user credential secret reads."""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import boto3

logger = logging.getLogger(__name__)
SECRET_PREFIX = "gym-booker/user/"


def _table():
    return boto3.resource("dynamodb").Table(os.environ["BOOKINGS_TABLE"])


def booking_id_for(option: dict) -> str:
    return f"{option['date']}_{option['time'].replace(':', '')}_{option['class_id']}"


def create_booking(*, telegram_user_id: str, chat_id: str, option: dict,
                   window_minutes: int, schedule_name: str, fire_at: str) -> str:
    booking_id = booking_id_for(option)
    _table().put_item(Item={
        "telegram_user_id": str(telegram_user_id),
        "booking_id": booking_id,
        "chat_id": chat_id,
        "class_id": option["class_id"],
        "session_id": option["session_id"],
        "date": option["date"],
        "time": option["time"],
        "name": option["name"],
        "studio": option.get("studio", ""),
        "window_minutes": window_minutes,
        "schedule_name": schedule_name,
        "fire_at": fire_at,
        "status": "scheduled",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return booking_id


def get_booking(telegram_user_id: str, booking_id: str) -> Optional[dict]:
    resp = _table().get_item(Key={"telegram_user_id": str(telegram_user_id),
                                  "booking_id": booking_id})
    return resp.get("Item")


def list_bookings(telegram_user_id: str) -> list[dict]:
    resp = _table().query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("telegram_user_id").eq(str(telegram_user_id))
    )
    return resp.get("Items", [])


def set_status(telegram_user_id: str, booking_id: str, status: str) -> None:
    _table().update_item(
        Key={"telegram_user_id": str(telegram_user_id), "booking_id": booking_id},
        UpdateExpression="SET #s = :s, updated_at = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": status,
                                   ":t": datetime.now(timezone.utc).isoformat()},
    )


def delete_booking(telegram_user_id: str, booking_id: str) -> None:
    _table().delete_item(Key={"telegram_user_id": str(telegram_user_id),
                              "booking_id": booking_id})


def get_user_credentials(telegram_user_id: str) -> dict:
    """Read the per-user secret: {email, password, api_key, club_id}."""
    sm = boto3.client("secretsmanager")
    raw = sm.get_secret_value(SecretId=f"{SECRET_PREFIX}{telegram_user_id}")["SecretString"]
    return json.loads(raw)
