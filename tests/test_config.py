from __future__ import annotations

from falafacil.config import DEFAULT_MODEL, MODEL_CHOICES, Settings


def test_default_model_and_choices() -> None:
    assert DEFAULT_MODEL == "gemini-3.5-flash-lite"
    assert MODEL_CHOICES == (
        ("gemini-3.5-flash-lite", "Econômico e rápido — Gemini 3.5 Flash-Lite"),
        ("gemini-3.7-flash", "Qualidade — Gemini 3.7 Flash"),
        ("gemini-3.8-flash", "Mais capaz — Gemini 3.8 Flash"),
    )


def test_settings_reads_model_and_environment_precedence(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test-opaque-id")
    monkeypatch.setenv("GEMINI_API_KEY", "environment-token")
    monkeypatch.setenv("GOOGLE_API_KEY", "fallback-environment-token")

    settings = Settings.from_env(
        fallback_api_key="persisted-token",
        fallback_model="gemini-3.5-flash-lite",
    )

    assert settings.model == "gemini-test-opaque-id"
    assert settings.model_from_environment is True
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

    settings = Settings.from_env(
        fallback_api_key="persisted-token",
        fallback_model="gemini-3.5-flash-lite",
    )

    assert settings.model == "gemini-3.5-flash-lite"
    assert settings.model_from_environment is False
    assert settings.api_key == "persisted-token"
    assert settings.has_api_key


def test_settings_uses_persisted_fallback_model_gemini_3_8_flash(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    settings = Settings.from_env(fallback_model="gemini-3.8-flash")
    assert settings.model == "gemini-3.8-flash"
    assert settings.model_from_environment is False


def test_settings_empty_environment_model_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_MODEL", "")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    settings = Settings.from_env(fallback_model="gemini-3.7-flash")

    assert settings.model == "gemini-3.7-flash"
    assert settings.model_from_environment is False

    settings_default = Settings.from_env()
    assert settings_default.model == DEFAULT_MODEL
    assert settings_default.model_from_environment is False


def test_settings_defaults_without_secret(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    settings = Settings.from_env()

    assert settings.model == DEFAULT_MODEL
    assert settings.model_from_environment is False
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


def test_settings_migrates_legacy_fallback_model(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    settings = Settings.from_env(fallback_model="gemini-2.5-flash-lite")

    assert settings.model == "gemini-3.5-flash-lite"
    assert settings.model == DEFAULT_MODEL
    assert settings.model_from_environment is False
