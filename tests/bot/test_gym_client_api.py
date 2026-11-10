"""gym_client HTTP behaviour, with `requests` mocked out."""

import base64
import json
from unittest import mock

import pytest
import requests

import gym_client


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GYM_API_BASE_URL", "https://api.example.test/v1")
    monkeypatch.setenv("GYM_APP_ID", "app-123")
    monkeypatch.setenv("GYM_API_KEY", "app-key")


def _token(**claims) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{body}.s"


def _resp(status=200, payload=None, text=""):
    r = mock.Mock()
    r.status_code = status
    r.text = text
    r.json.return_value = payload if payload is not None else {}
    r.raise_for_status.side_effect = (
        None if status < 400 else requests.HTTPError(f"HTTP {status}"))
    return r


def test_login_returns_token(monkeypatch):
    post = mock.Mock(return_value=_resp(200, {"token": "tok"}))
    monkeypatch.setattr(gym_client.requests, "post", post)
    assert gym_client.login("a@b.c", "pw", "k") == "tok"
    assert post.call_args.args[0] == "https://api.example.test/v1/sessions"


def test_login_rejected_returns_none(monkeypatch):
    monkeypatch.setattr(gym_client.requests, "post", mock.Mock(return_value=_resp(401)))
    assert gym_client.login("a@b.c", "bad", "k") is None


def test_login_network_error_returns_none(monkeypatch):
    monkeypatch.setattr(gym_client.requests, "post",
                        mock.Mock(side_effect=requests.ConnectionError()))
    assert gym_client.login("a@b.c", "pw", "k") is None


def test_validate_reports_priority_from_token_claim(monkeypatch):
    tok = _token(pp="true")
    monkeypatch.setattr(gym_client, "login", mock.Mock(return_value=tok))
    assert gym_client.validate("a@b.c", "pw") == {"ok": True, "priority": True}


def test_validate_failure(monkeypatch):
    monkeypatch.setattr(gym_client, "login", mock.Mock(return_value=None))
    assert gym_client.validate("a@b.c", "bad") == {"ok": False}


def test_claims_of_malformed_token_is_empty():
    assert gym_client._claims("not-a-jwt") == {}


def test_auth_headers_carry_member_ids():
    headers = gym_client._auth_headers(_token(mid=7, xid="x1"), "k")
    assert headers["X-Auth-Token"].count(".") == 2
    assert headers["X-Member-Id"] == "7"
    assert headers["X-Member-External-Id"] == "x1"


def _days():
    return [{"date": "2026-11-10", "classes": [
        {"id": 1, "sessionId": 11, "classTime": "18:30:00", "name": "Pilates",
         "studioName": "S1", "bookingCount": 3, "bookingCapacity": 10,
         "bookableStatus": "Bookable"},
        {"id": 2, "sessionId": 22, "classTime": "19:00:00", "name": "Spin",
         "bookableStatus": "Full"},
        {"id": 3, "sessionId": 33, "classTime": "23:00:00", "name": "Late",
         "bookableStatus": "Bookable"},
    ]}, {"date": "bad-date", "classes": []}]


def test_list_classes_filters_full_and_out_of_range(monkeypatch):
    monkeypatch.setattr(gym_client.requests, "get",
                        mock.Mock(return_value=_resp(200, _days())))
    opts = gym_client.list_classes(_token(mid=1, xid="x"), "k", "207",
                                   "2026-11-10", "18:00")
    assert [o["name"] for o in opts] == ["Pilates"]
    assert opts[0]["class_id"] == "1" and opts[0]["session_id"] == "11"


def test_list_classes_http_error_returns_empty(monkeypatch):
    monkeypatch.setattr(gym_client.requests, "get", mock.Mock(return_value=_resp(500)))
    assert gym_client.list_classes("t", "k", "207", "2026-11-10", "18:00") == []


@pytest.mark.parametrize("status,payload,expected", [
    (200, {}, ("success", "")),
    (201, {}, ("success", "")),
    (400, {"errorMessage": "TOO_EARLY_TO_BOOK"}, ("too_early", "TOO_EARLY_TO_BOOK")),
    (503, {}, ("error", "HTTP 503")),
    (409, {"errorMessage": "CLASS_FULL"}, ("rejected", "CLASS_FULL")),
])
def test_book_once_outcomes(monkeypatch, status, payload, expected):
    monkeypatch.setattr(gym_client.requests, "post",
                        mock.Mock(return_value=_resp(status, payload)))
    assert gym_client.book_once("t", "k", "207", "1", "11", "2026-11-10") == expected


def test_book_once_network_error(monkeypatch):
    monkeypatch.setattr(gym_client.requests, "post",
                        mock.Mock(side_effect=requests.Timeout()))
    outcome, detail = gym_client.book_once("t", "k", "207", "1", "11", "2026-11-10")
    assert outcome == "error" and "Timeout" in detail
