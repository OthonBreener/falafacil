from __future__ import annotations

import os
from dataclasses import dataclass, field



DEFAULT_MODEL = "gemini-3.7-flash"


@dataclass(frozen=True, slots=True)
class Settings:
    model: str = DEFAULT_MODEL
    api_key: str | None = field(default=None, repr=False, compare=False)

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def from_env(cls, fallback_api_key: str | None = None) -> "Settings":
        api_key = (
            os.getenv("GEMINI_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
            or fallback_api_key
            or None
        )
        return cls(
            model=os.getenv("GEMINI_MODEL", DEFAULT_MODEL),
            api_key=api_key,
        )

    @property
    def missing_api_key_message(self) -> str:
        return (
            "Configure a chave API pelo botão de configuração da interface "
            "ou defina GEMINI_API_KEY/GOOGLE_API_KEY no ambiente e reinicie o app "
            "para habilitar a transcrição."
        )
