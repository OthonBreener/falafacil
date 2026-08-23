from __future__ import annotations

import pytest

import falafacil.credentials as credentials
from falafacil.credentials import (
    ACCOUNT_NAME,
    SERVICE_NAME,
    CredentialStoreError,
    KeyringApiKeyStore,
)


class KeyringFake:
    def __init__(self, value: str | None = None) -> None:
        self.value = value
        self.calls: list[tuple[str, str, str | None]] = []

    def get_password(self, service: str, account: str) -> str | None:
        self.calls.append(("get", service, account))
        return self.value

    def set_password(self, service: str, account: str, value: str) -> None:
        self.calls.append(("set", service, account, value))
        self.value = value

    def delete_password(self, service: str, account: str) -> None:
        self.calls.append(("delete", service, account))
        self.value = None


def test_keyring_store_uses_exact_service_and_account(monkeypatch) -> None:
    backend = KeyringFake()
    monkeypatch.setattr(credentials, "keyring", backend)
    store = KeyringApiKeyStore()

    store.set_api_key("synthetic-storage-token")
    assert store.get_api_key() == "synthetic-storage-token"
    store.delete_api_key()

    assert backend.calls == [
        ("set", SERVICE_NAME, ACCOUNT_NAME, "synthetic-storage-token"),
        ("get", SERVICE_NAME, ACCOUNT_NAME),
        ("delete", SERVICE_NAME, ACCOUNT_NAME),
    ]
    assert SERVICE_NAME == "falafacil"
    assert ACCOUNT_NAME == "gemini-api-key"


def test_keyring_store_treats_missing_and_whitespace_as_absent(monkeypatch) -> None:
    backend = KeyringFake()
    monkeypatch.setattr(credentials, "keyring", backend)
    store = KeyringApiKeyStore()

    assert store.get_api_key() is None
    backend.value = " \t\n"
    assert store.get_api_key() is None

    store.set_api_key("")
    store.set_api_key(" \t\n")
    assert backend.calls == [
        ("get", SERVICE_NAME, ACCOUNT_NAME),
        ("get", SERVICE_NAME, ACCOUNT_NAME),
    ]


@pytest.mark.parametrize("operation", ["get_password", "set_password", "delete_password"])
def test_keyring_backend_errors_are_safe(monkeypatch, operation: str) -> None:
    secret = "synthetic-error-token"

    class BrokenBackend:
        def __getattribute__(self, name: str):
            if name == operation:
                def fail(*args, **kwargs):
                    raise RuntimeError(f"backend failed for {secret}")

                return fail
            return object.__getattribute__(self, name)

    monkeypatch.setattr(credentials, "keyring", BrokenBackend())
    store = KeyringApiKeyStore()

    with pytest.raises(CredentialStoreError) as caught:
        if operation == "get_password":
            store.get_api_key()
        elif operation == "set_password":
            store.set_api_key(secret)
        else:
            store.delete_api_key()

    assert secret not in str(caught.value)
    assert "chaveiro" in str(caught.value)
