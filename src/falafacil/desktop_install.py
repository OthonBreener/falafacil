"""User desktop entry installation for Homebrew and developer modes."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from . import __version__
from .homebrew_update import HomebrewUpdateError, load_homebrew_marker

DESKTOP_ENTRY_CATEGORIES = "Utility;AudioVideo;"


class DesktopInstallError(Exception):
    """Raised when desktop entry installation or validation fails."""


def _validate_path_characters(path_str: str, name: str) -> None:
    for char in path_str:
        if ord(char) < 32 or ord(char) == 127:
            raise DesktopInstallError(f"{name} contém caractere de controle inválido.")
        if ord(char) > 127:
            raise DesktopInstallError(f"{name} contém caractere não-ASCII '{char}'.")
        if char in {"%", "="}:
            raise DesktopInstallError(f"{name} contém caractere proibido '{char}'.")

def desktop_escape(value: str) -> str:
    """Escape path for quoted Exec command-line desktop entry field.

    Following freedesktop Desktop Entry Specification:
    - Raw backslash becomes \\\\\\\\ (4 backslashes in file)
    - Raw double quote becomes \\\\" (2 backslashes in file)
    - Raw backtick becomes \\\\` (2 backslashes in file)
    - Raw dollar sign becomes \\\\$ (2 backslashes in file)
    - Spaces, semicolons, and other characters are literal inside double quotes.
    """
    result = value.replace("\\", "\\\\\\\\")
    result = result.replace('"', '\\\\"')
    result = result.replace("`", "\\\\`")
    result = result.replace("$", "\\\\$")
    return result


def generic_escape(value: str) -> str:
    """Escape path for generic desktop entry field (e.g. TryExec).

    Following freedesktop Desktop Entry Specification:
    TryExec is a scalar string field (not a list), so only generic escapes
    apply. Backslash is escaped as \\\\; semicolons, spaces, quotes, backticks,
    and dollars are literal.
    """
    return value.replace("\\", "\\\\")


def _validate_user_dir_component(directory: Path) -> None:
    if not directory.exists():
        return
    try:
        st = directory.lstat()
    except OSError as exc:
        raise DesktopInstallError(
            f"Falha ao verificar diretório '{directory}': {exc}"
        ) from exc

    if directory.is_symlink():
        raise DesktopInstallError(
            f"Diretório de instalação contém componente symlink: '{directory}'."
        )
    if not stat.S_ISDIR(st.st_mode):
        raise DesktopInstallError(
            f"Componente de diretório não é um diretório: '{directory}'."
        )
    current_uid = os.getuid()
    if st.st_uid != current_uid:
        raise DesktopInstallError(
            f"Diretório '{directory}' pertence a outro usuário (UID {st.st_uid} != {current_uid})."
        )
    if (st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)) != 0:
        raise DesktopInstallError(
            f"Diretório '{directory}' possui permissões de escrita para grupo/outros."
        )
def _validate_developer_executable(executable: Path) -> None:
    try:
        st = executable.lstat()
    except OSError as exc:
        raise DesktopInstallError(
            f"Executável developer não encontrado ou inacessível: '{executable}': {exc}"
        ) from exc

    if executable.is_symlink():
        raise DesktopInstallError(
            f"Executável developer não pode ser um symlink: '{executable}'."
        )
    if not stat.S_ISREG(st.st_mode):
        raise DesktopInstallError(
            f"Executável developer deve ser um arquivo regular: '{executable}'."
        )
    if not os.access(executable, os.X_OK) or (st.st_mode & 0o111) == 0:
        raise DesktopInstallError(
            f"Executável developer não possui permissão de execução: '{executable}'."
        )
    current_uid = os.getuid()
    if st.st_uid != current_uid:
        raise DesktopInstallError(
            f"Executável developer não pertence ao usuário atual (UID {st.st_uid} != {current_uid})."
        )
    if (st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)) != 0:
        raise DesktopInstallError(
            f"Executável developer possui permissões de escrita para grupo/outros: '{executable}'."
        )

    home = Path.home()
    for parent in (home / ".local", home / ".local" / "bin"):
        _validate_user_dir_component(parent)


def _validate_homebrew_executable(executable: Path) -> None:
    try:
        resolved_exe = executable.resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise DesktopInstallError(
            f"Não foi possível resolver o executável Homebrew '{executable}': {exc}"
        ) from exc

    candidate_marker = resolved_exe.parent / "falafacil-homebrew.json"
    if not candidate_marker.is_file():
        raise DesktopInstallError(
            f"Executável '{executable}' não possui marker Homebrew adjacente em '{candidate_marker}'."
        )

    try:
        installation = load_homebrew_marker(candidate_marker, expected_version=__version__)
    except HomebrewUpdateError as exc:
        raise DesktopInstallError(
            f"Marker Homebrew inválido para executável '{executable}': {exc}"
        ) from exc

    if executable != installation.launch_path:
        raise DesktopInstallError(
            f"Executável '{executable}' não coincide com launch_path do Homebrew '{installation.launch_path}'."
        )


def install_user_desktop_entry(executable: Path) -> Path:
    """Validate executable and atomically install user desktop entry at ~/.local/share/applications/falafacil.desktop."""
    if isinstance(executable, str):
        executable = Path(executable)

    if not executable.is_absolute():
        raise DesktopInstallError(f"Caminho do executável deve ser absoluto: '{executable}'.")

    home = Path.home()
    _validate_path_characters(str(home), "Diretório HOME")
    _validate_path_characters(str(executable), "Caminho do executável")

    canonical_dev = home / ".local" / "bin" / "falafacil"
    if executable == canonical_dev:
        _validate_developer_executable(executable)
    else:
        _validate_homebrew_executable(executable)

    applications_dir = home / ".local" / "share" / "applications"
    desktop_entry_path = applications_dir / "falafacil.desktop"

    for comp in (home / ".local", home / ".local" / "share", applications_dir):
        if not comp.exists():
            try:
                comp.mkdir(mode=0o755)
                comp.chmod(0o755)
            except OSError as exc:
                raise DesktopInstallError(
                    f"Falha ao criar diretório de aplicações '{comp}': {exc}"
                ) from exc
        _validate_user_dir_component(comp)
    if desktop_entry_path.exists() or desktop_entry_path.is_symlink():
        try:
            dst_st = desktop_entry_path.lstat()
        except OSError as exc:
            raise DesktopInstallError(
                f"Falha ao inspecionar destino existente '{desktop_entry_path}': {exc}"
            ) from exc

        if desktop_entry_path.is_symlink():
            raise DesktopInstallError(
                f"Destino do desktop entry não pode ser um symlink: '{desktop_entry_path}'."
            )
        if not stat.S_ISREG(dst_st.st_mode):
            raise DesktopInstallError(
                f"Destino do desktop entry deve ser um arquivo regular: '{desktop_entry_path}'."
            )
        current_uid = os.getuid()
        if dst_st.st_uid != current_uid:
            raise DesktopInstallError(
                f"Destino '{desktop_entry_path}' pertence a outro usuário (UID {dst_st.st_uid} != {current_uid})."
            )
        if (dst_st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)) != 0:
            raise DesktopInstallError(
                f"Destino '{desktop_entry_path}' possui permissões de escrita para grupo/outros."
            )

    escaped_exec = desktop_escape(str(executable))
    escaped_tryexec = generic_escape(str(executable))
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=FalaFácil\n"
        "Comment=Transcrição de voz em português com Gemini\n"
        f'Exec="{escaped_exec}"\n'
        f"TryExec={escaped_tryexec}\n"
        "Terminal=false\n"
        f"Categories={DESKTOP_ENTRY_CATEGORIES}\n"
    )

    temp_path: Path | None = None
    try:
        descriptor, temp_path_str = tempfile.mkstemp(
            prefix=".falafacil.desktop.", dir=applications_dir
        )
        temp_path = Path(temp_path_str)
        with open(descriptor, "wb") as file_handle:
            file_handle.write(content.encode("utf-8"))
            file_handle.flush()
            os.fsync(file_handle.fileno())
            os.fchmod(file_handle.fileno(), 0o644)

        if desktop_entry_path.is_symlink():
            raise DesktopInstallError("Destino tornou-se um symlink durante a escrita.")

        os.replace(temp_path_str, desktop_entry_path)
        temp_path = None
        return desktop_entry_path
    except Exception as exc:
        if isinstance(exc, DesktopInstallError):
            raise
        raise DesktopInstallError(
            f"Falha na escrita atômica do desktop entry '{desktop_entry_path}': {exc}"
        ) from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
