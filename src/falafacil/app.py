from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from .config import Settings
from .credentials import CredentialStoreError, KeyringApiKeyStore
from .storage import LocalStore, resolve_storage_path
from .transcription import GeminiTranscriber
from .ui import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setOrganizationName("FalaFácil")
    app.setApplicationName("FalaFácil")
    local_store: LocalStore | None = None
    try:
        store_path = resolve_storage_path()
        local_store = LocalStore(store_path)
    except Exception:
        local_store = None


    api_key_store = KeyringApiKeyStore()
    try:
        persisted_api_key = api_key_store.get_api_key()
    except CredentialStoreError:
        persisted_api_key = None

    settings = Settings.from_env(fallback_api_key=persisted_api_key)

    def transcriber_factory(api_key: str) -> GeminiTranscriber:
        return GeminiTranscriber(api_key=api_key, model=settings.model)

    transcriber = (
        transcriber_factory(settings.api_key)
        if settings.has_api_key and settings.api_key is not None
        else None
    )
    window = MainWindow(
        settings=settings,
        transcriber=transcriber,
        api_key_store=api_key_store,
        transcriber_factory=transcriber_factory,
        local_store=local_store,
    )
    window.show()
    return app.exec()
