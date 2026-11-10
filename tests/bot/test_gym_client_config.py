"""The API host and app id are deployment config, never source defaults."""

import pytest

import gym_client


def test_base_url_comes_from_env(monkeypatch):
    monkeypatch.setenv("GYM_API_BASE_URL", "https://api.example.test/v1")
    assert gym_client._base_url() == "https://api.example.test/v1"


def test_base_url_is_required(monkeypatch):
    monkeypatch.delenv("GYM_API_BASE_URL", raising=False)
    with pytest.raises(KeyError):
        gym_client._base_url()


def test_app_id_header_comes_from_env(monkeypatch):
    monkeypatch.setenv("GYM_APP_ID", "app-123")
    assert gym_client._headers("key")["X-App-ID"] == "app-123"


def test_app_id_is_required(monkeypatch):
    monkeypatch.delenv("GYM_APP_ID", raising=False)
    with pytest.raises(KeyError):
        gym_client._headers("key")


def test_api_key_header_is_passed_through(monkeypatch):
    monkeypatch.setenv("GYM_APP_ID", "app-123")
    assert gym_client._headers("secret-key")["X-API-Key"] == "secret-key"
