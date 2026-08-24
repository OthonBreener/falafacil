from __future__ import annotations

import sys


def main() -> int:
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
