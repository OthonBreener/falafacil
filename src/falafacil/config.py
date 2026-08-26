from __future__ import annotations

import os
from dataclasses import dataclass, field



DEFAULT_MODEL = "gemini-3.5-flash-lite"

MODEL_CHOICES: tuple[tuple[str, str], ...] = (
    ("gemini-3.5-flash-lite", "Mais recente — Gemini 3.5 Flash-Lite"),
    ("gemini-3.7-flash", "Mais capaz — Gemini 3.7 Flash"),
)


@dataclass(frozen=True, slots=True)
class Settings:
    model: str = DEFAULT_MODEL
    api_key: str | None = field(default=None, repr=False, compare=False)
    model_from_environment: bool = False

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def from_env(
        cls,
        *,
        fallback_api_key: str | None = None,
        fallback_model: str | None = None,
    ) -> "Settings":
        api_key = (
            os.getenv("GEMINI_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
            or fallback_api_key
            or None
        )
        env_model = os.getenv("GEMINI_MODEL")
        if env_model is not None and env_model != "":
            model = env_model
            model_from_environment = True
        elif fallback_model is not None and fallback_model != "":
            if fallback_model == "gemini-2.5-flash-lite":
                model = DEFAULT_MODEL
            else:
                model = fallback_model
            model_from_environment = False
        else:
            model = DEFAULT_MODEL
            model_from_environment = False

        return cls(
            model=model,
            api_key=api_key,
            model_from_environment=model_from_environment,
        )

    @property
    def missing_api_key_message(self) -> str:
        return (
            "Configure a chave API pelo botão de configuração da interface "
            "ou defina GEMINI_API_KEY/GOOGLE_API_KEY no ambiente e reinicie o app "
            "para habilitar a transcrição."
        )
