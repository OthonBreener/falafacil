from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from . import __version__
from .config import Settings
from .credentials import CredentialStoreError, KeyringApiKeyStore
from .storage import LocalStore, resolve_storage_path
from .transcription import GeminiTranscriber
from .ui import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setOrganizationName("FalaFácil")
    app.setApplicationName("FalaFácil")
    app.setApplicationVersion(__version__)
    local_store: LocalStore | None = None
    try:
        store_path = resolve_storage_path()
        local_store = LocalStore(store_path)
    except Exception:
        local_store = None

    persisted_model: str | None = None
    if local_store is not None:
        try:
            persisted_model = local_store.get_gemini_model()
        except Exception:
            persisted_model = None

    api_key_store = KeyringApiKeyStore()
    try:
        persisted_api_key = api_key_store.get_api_key()
    except CredentialStoreError:
        persisted_api_key = None

    settings = Settings.from_env(
        fallback_api_key=persisted_api_key,
        fallback_model=persisted_model,
    )

    def transcriber_factory(api_key: str, model: str) -> GeminiTranscriber:
        return GeminiTranscriber(api_key=api_key, model=model)

    startup_message: str | None = None
    transcriber = None
    if settings.has_api_key and settings.api_key is not None:
        try:
            transcriber = transcriber_factory(settings.api_key, settings.model)
        except Exception:
            transcriber = None
            startup_message = (
                "Não foi possível iniciar o Gemini. Revise a chave ou o modelo nas Configurações."
            )
    homebrew_installation = None
    try:
        from .homebrew_update import detect_homebrew_installation

        homebrew_installation = detect_homebrew_installation()
    except Exception:
        homebrew_installation = None

    homebrew_update_controller = None
    if homebrew_installation is not None:
        try:
            from .homebrew_update import HomebrewUpdateController

            homebrew_update_controller = HomebrewUpdateController(homebrew_installation)
        except Exception:
            homebrew_update_controller = None

    window = MainWindow(
        settings=settings,
        transcriber=transcriber,
        api_key_store=api_key_store,
        transcriber_factory=transcriber_factory,
        local_store=local_store,
        homebrew_update_controller=homebrew_update_controller,
        startup_message=startup_message,
    )
    if homebrew_installation is not None:
        try:
            from .desktop_install import install_user_desktop_entry

            install_user_desktop_entry(homebrew_installation.launch_path)
        except Exception:
            pass
    window.show()
    return app.exec()
