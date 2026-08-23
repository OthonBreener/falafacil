from __future__ import annotations

from falafacil.config import DEFAULT_MODEL, Settings


def test_settings_reads_model_and_environment_precedence(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test")
    monkeypatch.setenv("GEMINI_API_KEY", "environment-token")
    monkeypatch.setenv("GOOGLE_API_KEY", "fallback-environment-token")

    settings = Settings.from_env(fallback_api_key="persisted-token")

    assert settings.model == "gemini-test"
    assert settings.api_key == "environment-token"
    assert settings.has_api_key


def test_settings_uses_google_key_before_persisted_fallback(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "google-token")

    settings = Settings.from_env(fallback_api_key="persisted-token")

    assert settings.api_key == "google-token"
    assert settings.has_api_key


def test_settings_uses_persisted_fallback_when_environment_is_absent(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    settings = Settings.from_env(fallback_api_key="persisted-token")

    assert settings.model == DEFAULT_MODEL
    assert settings.api_key == "persisted-token"
    assert settings.has_api_key


def test_settings_defaults_without_secret(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    settings = Settings.from_env()

    assert settings.model == DEFAULT_MODEL
    assert settings.api_key is None
    assert not settings.has_api_key
    assert "GEMINI_API_KEY" in settings.missing_api_key_message


def test_settings_redacts_api_key_from_repr_and_missing_message(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    secret = "synthetic-config-token"

    settings = Settings.from_env(fallback_api_key=secret)

    assert secret not in repr(settings)
    assert secret not in settings.missing_api_key_message
