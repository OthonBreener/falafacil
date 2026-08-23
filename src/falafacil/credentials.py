from __future__ import annotations

from typing import Protocol

try:
    import keyring
except ImportError:  # pragma: no cover - dependency is installed in production
    keyring = None  # type: ignore[assignment]


SERVICE_NAME = "falafacil"
ACCOUNT_NAME = "gemini-api-key"


class CredentialStoreError(RuntimeError):
    """Erro seguro ao acessar o chaveiro do sistema."""


class ApiKeyStore(Protocol):
    def get_api_key(self) -> str | None:
        """Retorna a chave persistida, ou ``None`` quando ela não existe."""

    def set_api_key(self, api_key: str) -> None:
        """Persiste uma chave não vazia no chaveiro do sistema."""

    def delete_api_key(self) -> None:
        """Remove a chave persistida do chaveiro do sistema."""


class KeyringApiKeyStore:
    """Implementa o armazenamento da chave usando o Secret Service via keyring."""

    def get_api_key(self) -> str | None:
        try:
            if keyring is None:
                raise RuntimeError("keyring indisponível")
            api_key = keyring.get_password(SERVICE_NAME, ACCOUNT_NAME)
        except Exception as exc:
            raise CredentialStoreError(
                "Não foi possível acessar o chaveiro do sistema."
            ) from exc
        return api_key if api_key and api_key.strip() else None

    def set_api_key(self, api_key: str) -> None:
        if not api_key or not api_key.strip():
            return
        try:
            if keyring is None:
                raise RuntimeError("keyring indisponível")
            keyring.set_password(SERVICE_NAME, ACCOUNT_NAME, api_key)
        except Exception as exc:
            raise CredentialStoreError(
                "Não foi possível salvar a chave no chaveiro do sistema."
            ) from exc

    def delete_api_key(self) -> None:
        try:
            if keyring is None:
                raise RuntimeError("keyring indisponível")
            keyring.delete_password(SERVICE_NAME, ACCOUNT_NAME)
        except Exception as exc:
            raise CredentialStoreError(
                "Não foi possível remover a chave do chaveiro do sistema."
            ) from exc
