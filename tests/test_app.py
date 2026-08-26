from __future__ import annotations

from typing import Any
import pytest

import falafacil
import falafacil.app as app_module
from falafacil.config import DEFAULT_MODEL
from falafacil.credentials import CredentialStoreError
from falafacil.storage import LocalStoreError


class FakeApp:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.org_name: str | None = None
        self.app_name: str | None = None
        self.app_version: str | None = None

    def setOrganizationName(self, name: str) -> None:
        self.org_name = name

    def setApplicationName(self, name: str) -> None:
        self.app_name = name

    def setApplicationVersion(self, version: str) -> None:
        self.app_version = version
    def exec(self) -> int:
        return 0


class FakeStoreForApp:
    def __init__(
        self,
        *,
        model: str | None = None,
        fail_get_model: bool = False,
    ) -> None:
        self._model = model
        self._fail_get_model = fail_get_model

    def get_gemini_model(self) -> str | None:
        if self._fail_get_model:
            raise LocalStoreError("falha ao ler modelo")
        return self._model


class FakeApiKeyStoreForApp:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        fail_get: bool = False,
    ) -> None:
        self._api_key = api_key
        self._fail_get = fail_get

    def get_api_key(self) -> str | None:
        if self._fail_get:
            raise CredentialStoreError("falha ao ler chaveiro")
        return self._api_key


class FakeTranscriberForApp:
    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model


class FakeMainWindowForApp:
    def __init__(
        self,
        *,
        settings: Any,
        transcriber: Any,
        api_key_store: Any,
        transcriber_factory: Any,
        local_store: Any,
        homebrew_update_controller: Any = None,
    ) -> None:
        self.settings = settings
        self.transcriber = transcriber
        self.api_key_store = api_key_store
        self.transcriber_factory = transcriber_factory
        self.local_store = local_store
        self.homebrew_update_controller = homebrew_update_controller
        self.shown = False

    def show(self) -> None:
        self.shown = True

@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.setattr(
        "falafacil.homebrew_update.detect_homebrew_installation",
        lambda: None,
    )

def test_main_startup_with_persisted_model_and_persisted_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_app = FakeApp([])
    monkeypatch.setattr(app_module, "QApplication", lambda argv: fake_app)

    fake_store = FakeStoreForApp(model="gemini-3.5-flash-lite")
    monkeypatch.setattr(app_module, "resolve_storage_path", lambda: "synthetic.db")
    monkeypatch.setattr(app_module, "LocalStore", lambda path: fake_store)

    fake_key_store = FakeApiKeyStoreForApp(api_key="persisted-secret-key")
    monkeypatch.setattr(app_module, "KeyringApiKeyStore", lambda: fake_key_store)

    transcriber_creations: list[tuple[str, str]] = []

    def fake_transcriber_init(api_key: str, model: str) -> FakeTranscriberForApp:
        transcriber_creations.append((api_key, model))
        return FakeTranscriberForApp(api_key=api_key, model=model)

    monkeypatch.setattr(app_module, "GeminiTranscriber", fake_transcriber_init)

    created_windows: list[FakeMainWindowForApp] = []
    monkeypatch.setattr(
        app_module,
        "MainWindow",
        lambda **kwargs: created_windows.append(FakeMainWindowForApp(**kwargs)) or created_windows[-1],
    )

    exit_code = app_module.main()

    assert exit_code == 0
    assert fake_app.org_name == "FalaFácil"
    assert fake_app.app_name == "FalaFácil"
    assert fake_app.app_version == "0.2.2"
    assert len(created_windows) == 1
    window = created_windows[0]
    assert window.shown is True
    assert window.local_store is fake_store
    assert window.api_key_store is fake_key_store

    assert window.settings.model == "gemini-3.5-flash-lite"
    assert window.settings.api_key == "persisted-secret-key"
    assert window.settings.model_from_environment is False

    assert transcriber_creations == [("persisted-secret-key", "gemini-3.5-flash-lite")]
    assert window.transcriber is not None
    assert window.transcriber.model == "gemini-3.5-flash-lite"
    assert window.transcriber.api_key == "persisted-secret-key"

    # Verify that the factory passed to MainWindow also uses (api_key, model)
    new_t = window.transcriber_factory("new-key", "gemini-3.7-flash")
    assert new_t.api_key == "new-key"
    assert new_t.model == "gemini-3.7-flash"


def test_main_startup_with_environment_override_precedes_persisted_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.7-flash")

    fake_app = FakeApp([])
    monkeypatch.setattr(app_module, "QApplication", lambda argv: fake_app)

    fake_store = FakeStoreForApp(model="gemini-3.5-flash-lite")
    monkeypatch.setattr(app_module, "resolve_storage_path", lambda: "synthetic.db")
    monkeypatch.setattr(app_module, "LocalStore", lambda path: fake_store)

    fake_key_store = FakeApiKeyStoreForApp(api_key="persisted-secret-key")
    monkeypatch.setattr(app_module, "KeyringApiKeyStore", lambda: fake_key_store)

    transcriber_creations: list[tuple[str, str]] = []
    monkeypatch.setattr(
        app_module,
        "GeminiTranscriber",
        lambda api_key, model: transcriber_creations.append((api_key, model))
        or FakeTranscriberForApp(api_key=api_key, model=model),
    )

    created_windows: list[FakeMainWindowForApp] = []
    monkeypatch.setattr(
        app_module,
        "MainWindow",
        lambda **kwargs: created_windows.append(FakeMainWindowForApp(**kwargs)) or created_windows[-1],
    )

    exit_code = app_module.main()

    assert exit_code == 0
    window = created_windows[0]
    assert window.settings.model == "gemini-3.7-flash"
    assert window.settings.model_from_environment is True
    assert transcriber_creations == [("persisted-secret-key", "gemini-3.7-flash")]
    assert window.transcriber.model == "gemini-3.7-flash"


def test_main_startup_with_opaque_environment_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_MODEL", "custom-opaque-id")

    fake_app = FakeApp([])
    monkeypatch.setattr(app_module, "QApplication", lambda argv: fake_app)

    fake_store = FakeStoreForApp(model="gemini-3.5-flash-lite")
    monkeypatch.setattr(app_module, "resolve_storage_path", lambda: "synthetic.db")
    monkeypatch.setattr(app_module, "LocalStore", lambda path: fake_store)

    fake_key_store = FakeApiKeyStoreForApp(api_key="persisted-secret-key")
    monkeypatch.setattr(app_module, "KeyringApiKeyStore", lambda: fake_key_store)

    transcriber_creations: list[tuple[str, str]] = []
    monkeypatch.setattr(
        app_module,
        "GeminiTranscriber",
        lambda api_key, model: transcriber_creations.append((api_key, model))
        or FakeTranscriberForApp(api_key=api_key, model=model),
    )

    created_windows: list[FakeMainWindowForApp] = []
    monkeypatch.setattr(
        app_module,
        "MainWindow",
        lambda **kwargs: created_windows.append(FakeMainWindowForApp(**kwargs)) or created_windows[-1],
    )

    exit_code = app_module.main()

    assert exit_code == 0
    window = created_windows[0]
    assert window.settings.model == "custom-opaque-id"
    assert window.settings.model_from_environment is True
    assert transcriber_creations == [("persisted-secret-key", "custom-opaque-id")]
    assert window.transcriber.model == "custom-opaque-id"


def test_main_startup_without_api_key_leaves_transcriber_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_app = FakeApp([])
    monkeypatch.setattr(app_module, "QApplication", lambda argv: fake_app)

    fake_store = FakeStoreForApp(model="gemini-3.5-flash-lite")
    monkeypatch.setattr(app_module, "resolve_storage_path", lambda: "synthetic.db")
    monkeypatch.setattr(app_module, "LocalStore", lambda path: fake_store)

    fake_key_store = FakeApiKeyStoreForApp(api_key=None)
    monkeypatch.setattr(app_module, "KeyringApiKeyStore", lambda: fake_key_store)

    transcriber_creations: list[tuple[str, str]] = []
    monkeypatch.setattr(
        app_module,
        "GeminiTranscriber",
        lambda api_key, model: transcriber_creations.append((api_key, model))
        or FakeTranscriberForApp(api_key=api_key, model=model),
    )

    created_windows: list[FakeMainWindowForApp] = []
    monkeypatch.setattr(
        app_module,
        "MainWindow",
        lambda **kwargs: created_windows.append(FakeMainWindowForApp(**kwargs)) or created_windows[-1],
    )

    exit_code = app_module.main()

    assert exit_code == 0
    window = created_windows[0]
    assert window.settings.has_api_key is False
    assert window.transcriber is None
    assert len(transcriber_creations) == 0


def test_main_startup_with_legacy_stored_model_migrates_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_app = FakeApp([])
    monkeypatch.setattr(app_module, "QApplication", lambda argv: fake_app)

    fake_store = FakeStoreForApp(model="gemini-2.5-flash-lite")
    monkeypatch.setattr(app_module, "resolve_storage_path", lambda: "synthetic.db")
    monkeypatch.setattr(app_module, "LocalStore", lambda path: fake_store)

    fake_key_store = FakeApiKeyStoreForApp(api_key="persisted-secret-key")
    monkeypatch.setattr(app_module, "KeyringApiKeyStore", lambda: fake_key_store)

    transcriber_creations: list[tuple[str, str]] = []
    monkeypatch.setattr(
        app_module,
        "GeminiTranscriber",
        lambda api_key, model: transcriber_creations.append((api_key, model))
        or FakeTranscriberForApp(api_key=api_key, model=model),
    )

    created_windows: list[FakeMainWindowForApp] = []
    monkeypatch.setattr(
        app_module,
        "MainWindow",
        lambda **kwargs: created_windows.append(FakeMainWindowForApp(**kwargs)) or created_windows[-1],
    )

    exit_code = app_module.main()

    assert exit_code == 0
    window = created_windows[0]
    assert window.settings.model == DEFAULT_MODEL
    assert window.settings.model == "gemini-3.5-flash-lite"
    assert window.settings.model_from_environment is False
    assert transcriber_creations == [("persisted-secret-key", "gemini-3.5-flash-lite")]
    assert window.transcriber.model == "gemini-3.5-flash-lite"


def test_main_startup_local_store_init_failure_results_in_none_and_default_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_app = FakeApp([])
    monkeypatch.setattr(app_module, "QApplication", lambda argv: fake_app)

    def failing_store_init(path: Any) -> Any:
        raise RuntimeError("cannot open sqlite database")

    monkeypatch.setattr(app_module, "resolve_storage_path", lambda: "synthetic.db")
    monkeypatch.setattr(app_module, "LocalStore", failing_store_init)

    fake_key_store = FakeApiKeyStoreForApp(api_key="persisted-key")
    monkeypatch.setattr(app_module, "KeyringApiKeyStore", lambda: fake_key_store)

    transcriber_creations: list[tuple[str, str]] = []
    monkeypatch.setattr(
        app_module,
        "GeminiTranscriber",
        lambda api_key, model: transcriber_creations.append((api_key, model))
        or FakeTranscriberForApp(api_key=api_key, model=model),
    )

    created_windows: list[FakeMainWindowForApp] = []
    monkeypatch.setattr(
        app_module,
        "MainWindow",
        lambda **kwargs: created_windows.append(FakeMainWindowForApp(**kwargs)) or created_windows[-1],
    )

    exit_code = app_module.main()

    assert exit_code == 0
    window = created_windows[0]
    assert window.local_store is None
    assert window.settings.model == DEFAULT_MODEL
    assert transcriber_creations == [("persisted-key", DEFAULT_MODEL)]


def test_main_startup_get_gemini_model_failure_retains_store_and_falls_back_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_app = FakeApp([])
    monkeypatch.setattr(app_module, "QApplication", lambda argv: fake_app)

    fake_store = FakeStoreForApp(fail_get_model=True)
    monkeypatch.setattr(app_module, "resolve_storage_path", lambda: "synthetic.db")
    monkeypatch.setattr(app_module, "LocalStore", lambda path: fake_store)

    fake_key_store = FakeApiKeyStoreForApp(api_key="persisted-key")
    monkeypatch.setattr(app_module, "KeyringApiKeyStore", lambda: fake_key_store)

    transcriber_creations: list[tuple[str, str]] = []
    monkeypatch.setattr(
        app_module,
        "GeminiTranscriber",
        lambda api_key, model: transcriber_creations.append((api_key, model))
        or FakeTranscriberForApp(api_key=api_key, model=model),
    )

    created_windows: list[FakeMainWindowForApp] = []
    monkeypatch.setattr(
        app_module,
        "MainWindow",
        lambda **kwargs: created_windows.append(FakeMainWindowForApp(**kwargs)) or created_windows[-1],
    )

    exit_code = app_module.main()

    assert exit_code == 0
    window = created_windows[0]
    # Crucial assertion: local_store is NOT None when only get_gemini_model() fails
    assert window.local_store is fake_store
    assert window.settings.model == DEFAULT_MODEL
    assert transcriber_creations == [("persisted-key", DEFAULT_MODEL)]

def test_main_startup_homebrew_registers_desktop_entry_before_show(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path
    from falafacil.homebrew_update import HomebrewInstallation

    fake_app = FakeApp([])
    monkeypatch.setattr(app_module, "QApplication", lambda argv: fake_app)
    monkeypatch.setattr(app_module, "resolve_storage_path", lambda: "synthetic.db")
    monkeypatch.setattr(app_module, "LocalStore", lambda path: None)
    monkeypatch.setattr(app_module, "KeyringApiKeyStore", lambda: FakeApiKeyStoreForApp(api_key=None))

    fake_installation = HomebrewInstallation(
        version=falafacil.__version__,
        formula="OthonBreener/falafacil/falafacil",
        homebrew_prefix=Path("/home/linuxbrew/.linuxbrew"),
        brew_path=Path("/home/linuxbrew/.linuxbrew/bin/brew"),
        launch_path=Path("/home/linuxbrew/.linuxbrew/opt/falafacil/bin/falafacil"),
        marker_path=Path(
            "/home/linuxbrew/.linuxbrew/opt/falafacil/libexec/falafacil-homebrew.json"
        ),
    )
    monkeypatch.setattr(
        "falafacil.homebrew_update.detect_homebrew_installation",
        lambda: fake_installation,
    )

    installed_paths: list[Path] = []
    show_called_before_install = False

    created_windows: list[TrackingMainWindow] = []

    class TrackingMainWindow(FakeMainWindowForApp):
        def show(self) -> None:
            nonlocal show_called_before_install
            if not installed_paths:
                show_called_before_install = True
            super().show()

    def make_tracking_window(**kwargs: Any) -> TrackingMainWindow:
        window = TrackingMainWindow(**kwargs)
        created_windows.append(window)
        return window

    monkeypatch.setattr(
        app_module,
        "MainWindow",
        make_tracking_window,
    )

    def fake_install_desktop(executable: Path) -> Path:
        installed_paths.append(executable)
        return Path("/tmp/fake.desktop")

    monkeypatch.setattr(
        "falafacil.desktop_install.install_user_desktop_entry",
        fake_install_desktop,
    )

    exit_code = app_module.main()
    assert exit_code == 0
    assert installed_paths == [fake_installation.launch_path]
    assert not show_called_before_install
    assert len(created_windows) == 1
    assert created_windows[0].homebrew_update_controller is not None
    assert (
        created_windows[0].homebrew_update_controller._installation
        == fake_installation
    )


def test_main_startup_source_mode_does_not_register_desktop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path

    fake_app = FakeApp([])
    monkeypatch.setattr(app_module, "QApplication", lambda argv: fake_app)
    monkeypatch.setattr(app_module, "resolve_storage_path", lambda: "synthetic.db")
    monkeypatch.setattr(app_module, "LocalStore", lambda path: None)
    monkeypatch.setattr(app_module, "KeyringApiKeyStore", lambda: FakeApiKeyStoreForApp(api_key=None))

    monkeypatch.setattr(
        "falafacil.homebrew_update.detect_homebrew_installation",
        lambda: None,
    )

    installed_paths: list[Path] = []
    monkeypatch.setattr(
        "falafacil.desktop_install.install_user_desktop_entry",
        lambda executable: installed_paths.append(executable),
    )

    created_windows: list[FakeMainWindowForApp] = []
    monkeypatch.setattr(
        app_module,
        "MainWindow",
        lambda **kwargs: created_windows.append(FakeMainWindowForApp(**kwargs)) or created_windows[-1],
    )

    exit_code = app_module.main()
    assert exit_code == 0
    assert len(installed_paths) == 0
    assert created_windows[0].shown is True
    assert created_windows[0].homebrew_update_controller is None


def test_main_startup_homebrew_registration_failure_is_fail_soft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path
    from falafacil.desktop_install import DesktopInstallError
    from falafacil.homebrew_update import HomebrewInstallation

    fake_app = FakeApp([])
    monkeypatch.setattr(app_module, "QApplication", lambda argv: fake_app)
    monkeypatch.setattr(app_module, "resolve_storage_path", lambda: "synthetic.db")
    monkeypatch.setattr(app_module, "LocalStore", lambda path: None)
    monkeypatch.setattr(app_module, "KeyringApiKeyStore", lambda: FakeApiKeyStoreForApp(api_key=None))

    fake_installation = HomebrewInstallation(
        version=falafacil.__version__,
        formula="OthonBreener/falafacil/falafacil",
        homebrew_prefix=Path("/home/linuxbrew/.linuxbrew"),
        brew_path=Path("/home/linuxbrew/.linuxbrew/bin/brew"),
        launch_path=Path("/home/linuxbrew/.linuxbrew/opt/falafacil/bin/falafacil"),
        marker_path=Path(
            "/home/linuxbrew/.linuxbrew/opt/falafacil/libexec/falafacil-homebrew.json"
        ),
    )
    monkeypatch.setattr(
        "falafacil.homebrew_update.detect_homebrew_installation",
        lambda: fake_installation,
    )

    def failing_install_desktop(executable: Path) -> Path:
        raise DesktopInstallError("permission denied")

    monkeypatch.setattr(
        "falafacil.desktop_install.install_user_desktop_entry",
        failing_install_desktop,
    )

    created_windows: list[FakeMainWindowForApp] = []
    monkeypatch.setattr(
        app_module,
        "MainWindow",
        lambda **kwargs: created_windows.append(FakeMainWindowForApp(**kwargs)) or created_windows[-1],
    )

    exit_code = app_module.main()
    assert exit_code == 0
    assert created_windows[0].homebrew_update_controller is not None


def test_main_startup_homebrew_detection_failure_is_fail_soft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path

    fake_app = FakeApp([])
    monkeypatch.setattr(app_module, "QApplication", lambda argv: fake_app)
    monkeypatch.setattr(app_module, "resolve_storage_path", lambda: "synthetic.db")
    monkeypatch.setattr(app_module, "LocalStore", lambda path: None)
    monkeypatch.setattr(
        app_module,
        "KeyringApiKeyStore",
        lambda: FakeApiKeyStoreForApp(api_key=None),
    )

    def failing_detect() -> Any:
        raise RuntimeError("detection crash")

    monkeypatch.setattr(
        "falafacil.homebrew_update.detect_homebrew_installation",
        failing_detect,
    )

    installed_paths: list[Path] = []
    monkeypatch.setattr(
        "falafacil.desktop_install.install_user_desktop_entry",
        lambda executable: installed_paths.append(executable),
    )

    created_windows: list[FakeMainWindowForApp] = []
    monkeypatch.setattr(
        app_module,
        "MainWindow",
        lambda **kwargs: created_windows.append(FakeMainWindowForApp(**kwargs))
        or created_windows[-1],
    )

    exit_code = app_module.main()
    assert exit_code == 0
    assert len(installed_paths) == 0
    assert created_windows[0].shown is True
    assert created_windows[0].homebrew_update_controller is None


def test_main_startup_homebrew_controller_init_failure_is_fail_soft_and_registers_desktop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path
    from falafacil.homebrew_update import HomebrewInstallation

    fake_app = FakeApp([])
    monkeypatch.setattr(app_module, "QApplication", lambda argv: fake_app)
    monkeypatch.setattr(app_module, "resolve_storage_path", lambda: "synthetic.db")
    monkeypatch.setattr(app_module, "LocalStore", lambda path: None)
    monkeypatch.setattr(
        app_module,
        "KeyringApiKeyStore",
        lambda: FakeApiKeyStoreForApp(api_key=None),
    )

    fake_installation = HomebrewInstallation(
        version=falafacil.__version__,
        formula="OthonBreener/falafacil/falafacil",
        homebrew_prefix=Path("/home/linuxbrew/.linuxbrew"),
        brew_path=Path("/home/linuxbrew/.linuxbrew/bin/brew"),
        launch_path=Path("/home/linuxbrew/.linuxbrew/opt/falafacil/bin/falafacil"),
        marker_path=Path(
            "/home/linuxbrew/.linuxbrew/opt/falafacil/libexec/falafacil-homebrew.json"
        ),
    )
    monkeypatch.setattr(
        "falafacil.homebrew_update.detect_homebrew_installation",
        lambda: fake_installation,
    )

    def failing_controller_init(installation: Any) -> Any:
        raise RuntimeError("controller init failed")

    monkeypatch.setattr(
        "falafacil.homebrew_update.HomebrewUpdateController",
        failing_controller_init,
    )

    installed_paths: list[Path] = []
    monkeypatch.setattr(
        "falafacil.desktop_install.install_user_desktop_entry",
        lambda executable: installed_paths.append(executable),
    )

    created_windows: list[FakeMainWindowForApp] = []
    monkeypatch.setattr(
        app_module,
        "MainWindow",
        lambda **kwargs: created_windows.append(FakeMainWindowForApp(**kwargs))
        or created_windows[-1],
    )

    exit_code = app_module.main()
    assert exit_code == 0
    assert installed_paths == [fake_installation.launch_path]
    assert created_windows[0].shown is True
    assert created_windows[0].homebrew_update_controller is None
    assert created_windows[0].shown is True
