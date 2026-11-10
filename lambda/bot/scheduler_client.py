"""EventBridge Scheduler wrapper: one-time schedules that invoke the booker."""

import json
import logging
import os
from datetime import timedelta

import boto3

import booking_core as core

logger = logging.getLogger(__name__)

FIRE_EARLY_MINUTES = 3  # invoke the booker this long before the window opens


def schedule_name_for(telegram_user_id: str, option: dict) -> str:
    return f"gym-{telegram_user_id}-{option['date']}-{option['time'].replace(':', '')}"


def schedule_or_book_now(*, telegram_user_id: str, booking_id: str,
                         option: dict, window_minutes: int) -> dict:
    """Create a one-time schedule at window_open - 3min (Europe/Rome).

    If that moment is already in the past, invoke the booker immediately
    (async) instead. Returns {"mode": "scheduled", "fire_at": ...} or
    {"mode": "immediate"}.
    """
    window_opens = core.window_opens_at(option["date"], option["time"], window_minutes)
    fire_at = window_opens - timedelta(minutes=FIRE_EARLY_MINUTES)
    payload = json.dumps({"telegram_user_id": str(telegram_user_id), "booking_id": booking_id})

    if fire_at <= core.now_rome():
        boto3.client("lambda").invoke(
            FunctionName=os.environ["BOOKER_FUNCTION_ARN"],
            InvocationType="Event",
            Payload=payload.encode(),
        )
        logger.info("Window imminent/open — invoked booker immediately for %s", booking_id)
        return {"mode": "immediate"}

    name = schedule_name_for(telegram_user_id, option)
    boto3.client("scheduler").create_schedule(
        Name=name,
        ScheduleExpression=f"at({fire_at.strftime('%Y-%m-%dT%H:%M:%S')})",
        ScheduleExpressionTimezone="Europe/Rome",
        FlexibleTimeWindow={"Mode": "OFF"},
        ActionAfterCompletion="DELETE",
        Target={
            "Arn": os.environ["BOOKER_FUNCTION_ARN"],
            "RoleArn": os.environ["SCHEDULER_ROLE_ARN"],
            "Input": payload,
            "RetryPolicy": {"MaximumRetryAttempts": 2, "MaximumEventAgeInSeconds": 300},
        },
    )
    logger.info("Scheduled %s at %s Europe/Rome", name, fire_at)
    return {"mode": "scheduled", "fire_at": fire_at.strftime("%Y-%m-%d %H:%M"),
            "schedule_name": name}


def delete_schedule(name: str) -> None:
    client = boto3.client("scheduler")
    try:
        client.delete_schedule(Name=name)
    except client.exceptions.ResourceNotFoundException:
        logger.info("Schedule %s already gone", name)
