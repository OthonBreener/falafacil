from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--update-probe":
        if len(sys.argv) != 3:
            return 2
        from falafacil import __version__

        return 0 if sys.argv[2] == __version__ else 1
    if len(sys.argv) >= 2 and sys.argv[1] == "--install-user-desktop":
        if len(sys.argv) != 3:
            return 2
        from pathlib import Path

        from falafacil.desktop_install import DesktopInstallError, install_user_desktop_entry

        try:
            install_user_desktop_entry(Path(sys.argv[2]))
            return 0
        except (DesktopInstallError, OSError):
            sys.stderr.write("Falha ao instalar desktop entry.\n")
            return 1
    if len(sys.argv) == 2 and sys.argv[1] == "--shortcut-daemon":
        from falafacil.shortcut_service import main as shortcut_daemon_main

        return shortcut_daemon_main()
    if len(sys.argv) == 2 and sys.argv[1] == "--install-shortcut-service":
        from falafacil.shortcut_install import install_privileged_service

        return install_privileged_service()
    from falafacil.app import main as app_main

    return app_main()


if __name__ == "__main__":
    raise SystemExit(main())
