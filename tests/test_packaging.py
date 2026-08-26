from __future__ import annotations

import hashlib
import importlib.metadata
import os
import re
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any, Sequence
import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_desktop.sh"


def run_installer(
    home: Path,
    source: Path,
    *,
    umask: int | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    if extra_env:
        environment.update(extra_env)
    if umask is not None:
        cmd = [
            "sh",
            "-c",
            f'umask {umask:04o} && exec sh "$1" "$2"',
            "sh",
            str(INSTALLER),
            str(source),
        ]
    else:
        cmd = ["sh", str(INSTALLER), str(source)]
    return subprocess.run(
        cmd,
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _create_fake_launcher(path: Path, log_file: Path | None = None) -> Path:
    log_cmd = f'printf "LAUNCHED\\n" >> "{log_file}"\n' if log_file else ""
    content = f"""#!/bin/sh
{log_cmd}export PYTHONPATH="{ROOT / 'src'}"
exec "{sys.executable}" -m falafacil "$@"
"""
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def _create_instrumented_mkdir(tools_dir: Path, log_file: Path) -> None:
    tools_dir.mkdir(parents=True, exist_ok=True)
    mkdir_script = tools_dir / "mkdir"
    content = f"""#!/bin/sh
set -eu
LOG_FILE="{log_file}"
CURRENT_UMASK=$(umask)
/bin/mkdir "$@"
for arg in "$@"; do
    case "$arg" in
        -*) ;;
        *)
            if [ -d "$arg" ]; then
                MODE=$(stat -c "%a" "$arg")
                printf '%s %s %s\\n' "$CURRENT_UMASK" "$MODE" "$arg" >> "$LOG_FILE"
            fi
            ;;
    esac
done
"""
    mkdir_script.write_text(content, encoding="utf-8")
    mkdir_script.chmod(0o755)


def _create_fake_chmod_fail(tools_dir: Path) -> None:
    tools_dir.mkdir(parents=True, exist_ok=True)
    chmod_script = tools_dir / "chmod"
    content = """#!/bin/sh
set -eu
FAIL_TARGET="${FAIL_CHMOD_TARGET:-}"
for arg in "$@"; do
    case "$arg" in
        -*) ;;
        *)
            if [ -e "$arg" ]; then
                BASENAME=$(basename -- "$arg")
                if [ "$FAIL_TARGET" = "local" ] && [ "$BASENAME" = ".local" ]; then
                    printf 'chmod: simulated failure on %s\\n' "$arg" >&2
                    exit 1
                elif [ "$FAIL_TARGET" = "bin" ] && [ "$BASENAME" = "bin" ]; then
                    printf 'chmod: simulated failure on %s\\n' "$arg" >&2
                    exit 1
                elif [ "$FAIL_TARGET" = "temp_exec" ] && case "$BASENAME" in .falafacil.*) true ;; *) false ;; esac; then
                    printf 'chmod: simulated failure on %s\\n' "$arg" >&2
                    exit 1
                fi
            fi
            ;;
    esac
done
exec /bin/chmod "$@"
"""
    chmod_script.write_text(content, encoding="utf-8")
    chmod_script.chmod(0o755)

def test_installer_copies_executable_and_writes_safe_desktop_entry(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = _create_fake_launcher(tmp_path / "fake-launcher")

    result = run_installer(home, source)
    assert result.returncode == 0, result.stderr
    installed = home / ".local" / "bin" / "falafacil"
    desktop = home / ".local" / "share" / "applications" / "falafacil.desktop"
    assert installed.is_file()
    assert stat.S_IMODE(installed.stat().st_mode) == 0o755
    assert installed.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")

    content = desktop.read_text(encoding="utf-8")
    expected_path = str(installed)
    assert f'Exec="{expected_path}"' in content
    assert f"TryExec={expected_path}" in content
    assert "Terminal=false" in content
    assert "Categories=Utility;AudioVideo;" in content
    assert "Type=Application" in content
    assert "Name=FalaFácil" in content
    assert "$HOME" not in content
    assert "~" not in content
    assert "sh -c" not in content
    assert "Environment=" not in content
    for marker in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "API_KEY", "api_key"):
        assert marker not in content


def test_installer_preserves_preexisting_private_directory_permissions(tmp_path: Path) -> None:
    home = tmp_path / "home"
    local_dir = home / ".local"
    bin_dir = local_dir / "bin"
    bin_dir.mkdir(parents=True)
    local_dir.chmod(0o700)
    bin_dir.chmod(0o700)

    source = _create_fake_launcher(tmp_path / "fake-launcher")
    result = run_installer(home, source)

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(local_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(bin_dir.stat().st_mode) == 0o700
    installed = bin_dir / "falafacil"
    assert installed.is_file()
    assert stat.S_IMODE(installed.stat().st_mode) == 0o755


def test_installer_preserves_preexisting_local_dir_and_creates_secure_bin_dir(tmp_path: Path) -> None:
    home = tmp_path / "home"
    local_dir = home / ".local"
    local_dir.mkdir(parents=True)
    local_dir.chmod(0o700)

    source = _create_fake_launcher(tmp_path / "fake-launcher")
    result = run_installer(home, source)

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(local_dir.stat().st_mode) == 0o700
    bin_dir = local_dir / "bin"
    assert stat.S_IMODE(bin_dir.stat().st_mode) == 0o755


def test_installer_creates_secure_directories_under_permissive_umask(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = _create_fake_launcher(tmp_path / "fake-launcher")

    result = run_installer(home, source, umask=0o000)

    assert result.returncode == 0, result.stderr
    local_dir = home / ".local"
    bin_dir = local_dir / "bin"
    installed = bin_dir / "falafacil"

    assert local_dir.is_dir()
    assert bin_dir.is_dir()
    assert installed.is_file()

    assert stat.S_IMODE(local_dir.stat().st_mode) == 0o755
    assert stat.S_IMODE(bin_dir.stat().st_mode) == 0o755
    assert stat.S_IMODE(installed.stat().st_mode) == 0o755
    assert (local_dir.stat().st_mode & 0o022) == 0
    assert (bin_dir.stat().st_mode & 0o022) == 0


@pytest.mark.parametrize(
    ("initial_mode", "expected_mode"),
    [
        (0o770, 0o750),
        (0o777, 0o755),
        (0o757, 0o755),
        (0o775, 0o755),
        (0o700, 0o700),
        (0o750, 0o750),
        (0o755, 0o755),
    ],
    ids=["0770->0750", "0777->0755", "0757->0755", "0775->0755", "0700->0700", "0750->0750", "0755->0755"],
)
def test_installer_strips_group_other_write_without_broadening_other_bits(
    tmp_path: Path,
    initial_mode: int,
    expected_mode: int,
) -> None:
    home = tmp_path / "home"
    local_dir = home / ".local"
    bin_dir = local_dir / "bin"
    bin_dir.mkdir(parents=True)
    local_dir.chmod(initial_mode)
    bin_dir.chmod(initial_mode)

    source = _create_fake_launcher(tmp_path / "fake-launcher")
    result = run_installer(home, source)

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(local_dir.stat().st_mode) == expected_mode
    assert stat.S_IMODE(bin_dir.stat().st_mode) == expected_mode
    assert (local_dir.stat().st_mode & 0o022) == 0
    assert (bin_dir.stat().st_mode & 0o022) == 0

def test_installer_creates_directories_with_safe_umask_instrumented(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    tools_dir = tmp_path / "tools"
    audit_log = tmp_path / "mkdir_audit.log"
    _create_instrumented_mkdir(tools_dir, audit_log)

    source = _create_fake_launcher(tmp_path / "fake-launcher")
    result = run_installer(
        home,
        source,
        umask=0o000,
        extra_env={"PATH": f"{tools_dir}:{os.environ.get('PATH', '')}"},
    )

    assert result.returncode == 0, result.stderr
    assert audit_log.is_file()

    entries = [
        line.strip().split(maxsplit=2)
        for line in audit_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(entries) >= 2, f"Expected at least .local and bin entries, got: {entries}"

    for umask_str, mode_str, dir_path in entries:
        observed_umask = int(umask_str, 8)
        assert (observed_umask & 0o022) == 0o022, f"Umask {umask_str} did not mask write bits"
        assert umask_str in ("0077", "077", "77"), f"Expected umask 0077, got {umask_str}"
        observed_mode = int(mode_str, 8)
        assert (observed_mode & 0o022) == 0, f"Directory {dir_path} had group/other write permissions at creation: {mode_str}"
        assert observed_mode == 0o700, f"Expected initial directory mode 0700, got {mode_str} for {dir_path}"

    local_dir = home / ".local"
    bin_dir = local_dir / "bin"
    installed = bin_dir / "falafacil"
    assert stat.S_IMODE(local_dir.stat().st_mode) == 0o755
    assert stat.S_IMODE(bin_dir.stat().st_mode) == 0o755
    assert stat.S_IMODE(installed.stat().st_mode) == 0o755


@pytest.mark.parametrize(
    ("scenario", "precreate", "target_kind"),
    [
        ("new_local", False, "local"),
        ("existing_local", True, "local"),
        ("new_bin", False, "bin"),
        ("existing_bin", True, "bin"),
    ],
    ids=["new_local_chmod_fails", "existing_local_chmod_fails", "new_bin_chmod_fails", "existing_bin_chmod_fails"],
)
def test_installer_fails_closed_when_directory_chmod_fails(
    tmp_path: Path,
    scenario: str,
    precreate: bool,
    target_kind: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    if precreate:
        if "bin" in scenario:
            (home / ".local" / "bin").mkdir(parents=True)
            (home / ".local").chmod(0o777)
            (home / ".local" / "bin").chmod(0o777)
        else:
            (home / ".local").mkdir(parents=True)
            (home / ".local").chmod(0o777)

    tools_dir = tmp_path / "tools"
    _create_fake_chmod_fail(tools_dir)

    launcher_log = tmp_path / "launcher.log"
    source = _create_fake_launcher(tmp_path / "fake-launcher", log_file=launcher_log)

    result = run_installer(
        home,
        source,
        extra_env={
            "PATH": f"{tools_dir}:{os.environ.get('PATH', '')}",
            "FAIL_CHMOD_TARGET": target_kind,
        },
    )

    assert result.returncode != 0
    assert "simulated failure" in result.stderr
    installed = home / ".local" / "bin" / "falafacil"
    desktop = home / ".local" / "share" / "applications" / "falafacil.desktop"
    assert not installed.exists()
    assert not desktop.exists()
    assert not launcher_log.exists()
    if (home / ".local" / "bin").exists():
        temp_files = list((home / ".local" / "bin").glob(".falafacil.*"))
        assert temp_files == []


def test_installer_fails_closed_when_temp_exec_chmod_fails(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    tools_dir = tmp_path / "tools"
    _create_fake_chmod_fail(tools_dir)

    launcher_log = tmp_path / "launcher.log"
    source = _create_fake_launcher(tmp_path / "fake-launcher", log_file=launcher_log)

    result = run_installer(
        home,
        source,
        extra_env={
            "PATH": f"{tools_dir}:{os.environ.get('PATH', '')}",
            "FAIL_CHMOD_TARGET": "temp_exec",
        },
    )

    assert result.returncode != 0
    assert "simulated failure" in result.stderr
    installed = home / ".local" / "bin" / "falafacil"
    desktop = home / ".local" / "share" / "applications" / "falafacil.desktop"
    assert not installed.exists()
    assert not desktop.exists()
    assert not launcher_log.exists()
    if (home / ".local" / "bin").exists():
        temp_files = list((home / ".local" / "bin").glob(".falafacil.*"))
        assert temp_files == []

def _decode_generic_string(value: str) -> str:
    decoded: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character != "\\":
            decoded.append(character)
            index += 1
            continue
        assert index + 1 < len(value), "Dangling backslash at end of generic desktop entry string"
        escaped = value[index + 1]
        if escaped == "\\":
            decoded.append("\\")
        elif escaped == "s":
            decoded.append(" ")
        elif escaped == "n":
            decoded.append("\n")
        elif escaped == "t":
            decoded.append("\t")
        elif escaped == "r":
            decoded.append("\r")
        elif escaped == ";":
            decoded.append(";")
        else:
            raise AssertionError(f"Invalid generic desktop entry escape sequence '\\{escaped}'.")
        index += 2
    return "".join(decoded)


def _decode_exec_quoted_argument(value: str) -> str:
    assert value.startswith('"') and value.endswith('"'), f"Exec argument must be quoted: {value}"
    inner = value[1:-1]
    # Stage 1: Generic unescaping
    generic_str = _decode_generic_string(inner)

    # Stage 2: Exec quote unescaping inside double quotes (\" -> ", \` -> `, \$ -> $, \\ -> \)
    exec_decoded: list[str] = []
    i = 0
    while i < len(generic_str):
        if generic_str[i] == "\\":
            assert i + 1 < len(generic_str), "Dangling backslash in Exec argument"
            esc = generic_str[i + 1]
            assert esc in {'"', "`", "$", "\\"}, f"Invalid Exec escape '\\{esc}'"
            exec_decoded.append(esc)
            i += 2
        else:
            exec_decoded.append(generic_str[i])
            i += 1
    return "".join(exec_decoded)

def test_installer_escapes_generic_tryexec_path(tmp_path) -> None:
    home = tmp_path / "home with spaces;and\\slashes"
    home.mkdir()
    source = _create_fake_launcher(tmp_path / "fake-launcher")

    result = run_installer(home, source)

    assert result.returncode == 0, result.stderr
    installed = home / ".local" / "bin" / "falafacil"
    desktop = home / ".local" / "share" / "applications" / "falafacil.desktop"
    content = desktop.read_text(encoding="utf-8")
    exec_value = next(
        line.removeprefix("Exec=")
        for line in content.splitlines()
        if line.startswith("Exec=")
    )
    assert _decode_exec_quoted_argument(exec_value) == str(installed)

    try_exec = next(
        line.removeprefix("TryExec=")
        for line in content.splitlines()
        if line.startswith("TryExec=")
    )

    assert '"' not in try_exec
    assert "\\;" not in try_exec
    assert ";" in try_exec
    assert "\\\\" in try_exec
    assert _decode_generic_string(try_exec) == str(installed)
    assert installed.is_file()


@pytest.mark.parametrize(
    "bad_char",
    ["\t", "\r", "\u00e1", "\u00e7"],
    ids=["tab", "carriage-return", "a-acute", "c-cedilla"],
)
def test_installer_rejects_unsafe_home_control_or_non_ascii_character(tmp_path, bad_char: str) -> None:
    home = tmp_path / f"home{bad_char}unsafe"
    home.mkdir()
    source = _create_fake_launcher(tmp_path / "fake-launcher")

    result = run_installer(home, source)

    assert result.returncode != 0
    assert not (home / ".local").exists()


@pytest.mark.parametrize("destination_kind", ["directory", "symlink"])
def test_installer_rejects_unsafe_existing_destination(tmp_path, destination_kind: str) -> None:
    home = tmp_path / "home"
    install_dir = home / ".local" / "bin"
    install_dir.mkdir(parents=True)
    destination = install_dir / "falafacil"
    if destination_kind == "directory":
        destination.mkdir()
    else:
        target = tmp_path / "existing-target"
        target.write_text("existing", encoding="utf-8")
        destination.symlink_to(target)

    source = _create_fake_launcher(tmp_path / "fake-launcher")

    result = run_installer(home, source)

    assert result.returncode != 0
    assert destination.exists() or destination.is_symlink()
    assert not (home / ".local" / "share" / "applications" / "falafacil.desktop").exists()


def test_module_dispatches_only_exact_internal_shortcut_modes(monkeypatch) -> None:
    from falafacil import __main__ as module_entry

    daemon_module = ModuleType("falafacil.shortcut_service")
    daemon_module.main = lambda: 21
    installer_module = ModuleType("falafacil.shortcut_install")
    installer_module.install_privileged_service = lambda: 22
    app_module = ModuleType("falafacil.app")
    app_module.main = lambda: 23
    monkeypatch.setitem(sys.modules, "falafacil.shortcut_service", daemon_module)
    monkeypatch.setitem(sys.modules, "falafacil.shortcut_install", installer_module)
    monkeypatch.setitem(sys.modules, "falafacil.app", app_module)

    monkeypatch.setattr(sys, "argv", ["falafacil", "--shortcut-daemon"])
    assert module_entry.main() == 21
    monkeypatch.setattr(sys, "argv", ["falafacil", "--install-shortcut-service"])
    assert module_entry.main() == 22
    monkeypatch.setattr(sys, "argv", ["falafacil", "--shortcut-daemon", "extra"])
    assert module_entry.main() == 23
    monkeypatch.setattr(sys, "argv", ["falafacil", "--unknown-internal-mode"])
    assert module_entry.main() == 23

def test_module_dispatches_install_user_desktop_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from falafacil import __main__ as module_entry

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    dev_bin = home / ".local" / "bin" / "falafacil"
    for parent in (home, home / ".local", home / ".local" / "bin"):
        parent.mkdir(parents=True, exist_ok=True)
        parent.chmod(0o755)
    dev_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    dev_bin.chmod(0o755)
    # 1. Exact valid arity succeeds and creates desktop entry
    monkeypatch.setattr(sys, "argv", ["falafacil", "--install-user-desktop", str(dev_bin)])
    assert module_entry.main() == 0
    desktop = home / ".local" / "share" / "applications" / "falafacil.desktop"
    assert desktop.is_file()

    # 2. Malformed arity missing argument returns 2 without GUI fallthrough
    app_called = False
    fake_app_mod = ModuleType("falafacil.app")
    fake_app_mod.main = lambda: (setattr(fake_app_mod, "called", True), 99)[1]
    monkeypatch.setitem(sys.modules, "falafacil.app", fake_app_mod)

    monkeypatch.setattr(sys, "argv", ["falafacil", "--install-user-desktop"])
    assert module_entry.main() == 2
    assert not getattr(fake_app_mod, "called", False)

    # 3. Malformed arity extra argument returns 2 without GUI fallthrough
    monkeypatch.setattr(
        sys,
        "argv",
        ["falafacil", "--install-user-desktop", str(dev_bin), "extra_arg"],
    )
    assert module_entry.main() == 2
    assert not getattr(fake_app_mod, "called", False)

    # 4. Valid arity with invalid/non-existent path returns 1 and emits only generic message
    monkeypatch.setattr(
        sys,
        "argv",
        ["falafacil", "--install-user-desktop", str(tmp_path / "non_existent")],
    )
    assert module_entry.main() == 1
    captured = capsys.readouterr()
    assert captured.err == "Falha ao instalar desktop entry.\n"
    assert "non_existent" not in captured.err

    # 5. Internal exception with sensitive sentinel data never leaks to stderr
    def fake_failing_install(_path):
        from falafacil.desktop_install import DesktopInstallError
        raise DesktopInstallError("SECRET_SENTINEL_TOKEN_XYZ_12345: /untrusted/leak/path")

    monkeypatch.setattr("falafacil.desktop_install.install_user_desktop_entry", fake_failing_install)
    monkeypatch.setattr(
        sys,
        "argv",
        ["falafacil", "--install-user-desktop", str(dev_bin)],
    )
    assert module_entry.main() == 1
    captured_secret = capsys.readouterr()
    assert captured_secret.err == "Falha ao instalar desktop entry.\n"
    assert "SECRET_SENTINEL_TOKEN_XYZ_12345" not in captured_secret.err
    assert "/untrusted/leak/path" not in captured_secret.err

def test_pyproject_requires_evdev_two_point_zero_floor() -> None:
    pyproject_path = ROOT / "pyproject.toml"
    with pyproject_path.open("rb") as fp:
        data = tomllib.load(fp)

    dependencies: list[str] = data.get("project", {}).get("dependencies", [])
    evdev_spec = next((dep for dep in dependencies if dep.startswith("evdev")), None)
    assert evdev_spec is not None, "evdev dependency not found in pyproject.toml"

    match = re.match(r"^evdev\s*>=\s*([0-9]+(?:\.[0-9]+)*)", evdev_spec)
    assert match is not None, f"Unexpected evdev spec format: {evdev_spec}"
    floor_version_tuple = tuple(int(part) for part in match.group(1).split("."))
    while len(floor_version_tuple) < 3:
        floor_version_tuple = (*floor_version_tuple, 0)

    assert floor_version_tuple >= (2, 0, 0), (
        f"Declared evdev minimum floor '{evdev_spec}' is below 2.0.0; "
        "InputDeviceMonitor requires evdev>=2.0.0 for list_devices(writable=False)."
    )
    assert evdev_spec == "evdev>=2.0.0"

    agents_path = ROOT / "AGENTS.md"
    agents_content = agents_path.read_text(encoding="utf-8")
    assert "evdev>=2.0.0" in agents_content
    assert "evdev>=1.7" not in agents_content


def test_single_source_of_truth_version_and_pyproject_dynamic_metadata() -> None:
    import falafacil

    assert falafacil.__version__ == "0.2.1"
    assert importlib.metadata.version("falafacil") == "0.2.1"

    pyproject_path = ROOT / "pyproject.toml"
    with pyproject_path.open("rb") as fp:
        data = tomllib.load(fp)

    build_system = data.get("build-system", {})
    assert build_system.get("build-backend") == "setuptools.build_meta"

    project_table = data.get("project", {})
    assert "version" not in project_table, "pyproject.toml deve usar dynamic version em vez de version estático"
    assert project_table.get("dynamic") == ["version"], "pyproject.toml deve declarar dynamic = ['version']"

    setuptools_dynamic = data.get("tool", {}).get("setuptools", {}).get("dynamic", {})
    assert setuptools_dynamic.get("version") == {"attr": "falafacil.__version__"}

    poetry_table = data.get("tool", {}).get("poetry", {})
    assert poetry_table.get("package-mode") is False, "tool.poetry deve usar package-mode = false para gerenciar apenas dependências"
    assert "version" not in poetry_table, "tool.poetry não deve declarar version estático duplicado"
    assert "packages" not in poetry_table, "tool.poetry não deve empacotar o projeto raiz quando package-mode é false"

    scripts_table = project_table.get("scripts", {})
    assert scripts_table.get("falafacil") == "falafacil.__main__:main", "entry point falafacil deve apontar para __main__:main"


def test_installed_distribution_metadata_and_console_script() -> None:
    import falafacil

    assert importlib.metadata.version("falafacil") == "0.2.1"
    assert importlib.metadata.version("falafacil") == falafacil.__version__

    entry_points = importlib.metadata.entry_points(group="console_scripts")
    falafacil_ep = next((ep for ep in entry_points if ep.name == "falafacil"), None)
    assert falafacil_ep is not None, "Console entry point 'falafacil' deve estar registrado nos metadados"
    assert falafacil_ep.value == "falafacil.__main__:main"

def test_clean_checkout_tag_validation_import_ordering() -> None:
    clean_env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT / "src"),
    }
    res = subprocess.run(
        [sys.executable, "-c", "import falafacil; print(falafacil.__version__)"],
        env=clean_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"Import falafacil failed with PYTHONPATH=src: {res.stderr}"
    assert res.stdout.strip() == "0.2.1"

def test_module_dispatches_update_probe_contract(monkeypatch) -> None:
    from falafacil import __main__ as module_entry

    app_called = False

    def fake_app_main() -> int:
        nonlocal app_called
        app_called = True
        return 0

    app_module = ModuleType("falafacil.app")
    app_module.main = fake_app_main
    monkeypatch.setitem(sys.modules, "falafacil.app", app_module)

    # Versão correspondente -> exit 0
    monkeypatch.setattr(sys, "argv", ["falafacil", "--update-probe", "0.2.1"])
    assert module_entry.main() == 0
    assert not app_called

    # Versão divergente -> exit 1
    monkeypatch.setattr(sys, "argv", ["falafacil", "--update-probe", "0.1.0"])
    assert module_entry.main() == 1
    monkeypatch.setattr(sys, "argv", ["falafacil", "--update-probe", "9.9.9"])
    assert module_entry.main() == 1
    assert not app_called

    # Aridade malformada -> exit 2
    monkeypatch.setattr(sys, "argv", ["falafacil", "--update-probe"])
    assert module_entry.main() == 2
    monkeypatch.setattr(sys, "argv", ["falafacil", "--update-probe", "0.2.1", "extra"])
    assert module_entry.main() == 2
    assert not app_called


def test_spec_bundles_portaudio_and_fails_when_absent() -> None:
    spec_path = ROOT / "packaging" / "falafacil.spec"
    assert spec_path.is_file()
    spec_content = spec_path.read_text(encoding="utf-8")

    assert '/usr/lib/x86_64-linux-gnu/libportaudio.so.2' in spec_content
    assert 'PORTAUDIO_PATH = Path("/usr/lib/x86_64-linux-gnu/libportaudio.so.2")' in spec_content
    assert 'if not PORTAUDIO_PATH.is_file():' in spec_content
    assert 'raise FileNotFoundError(' in spec_content
    assert 'binaries=[(str(PORTAUDIO_PATH), ".")]' in spec_content


def test_homebrew_formula_template_structure_and_placeholders() -> None:
    template_path = ROOT / "packaging" / "homebrew" / "falafacil.rb.in"
    assert template_path.is_file()
    content = template_path.read_text(encoding="utf-8")

    assert 'require "json"' in content
    assert 'class Falafacil < Formula' in content
    assert 'desc "Transcrição de voz em português com Gemini"' in content
    assert 'homepage "https://github.com/OthonBreener/falafacil"' in content
    assert 'url "https://github.com/OthonBreener/falafacil/releases/download/v@VERSION@/falafacil-@VERSION@-linux-x86_64.tar.gz"' in content
    assert 'version "@VERSION@"' not in content
    assert 'sha256 "@SHA256@"' in content
    assert 'depends_on arch: :x86_64' in content
    assert 'depends_on :linux' in content
    assert content.index('depends_on arch: :x86_64') < content.index('depends_on :linux')
    assert 'libexec.install "falafacil"' in content
    assert 'bin.install_symlink libexec/"falafacil"' in content
    assert 'schema:          1' in content
    assert 'channel:         "homebrew"' in content
    assert 'formula:         "OthonBreener/falafacil/falafacil"' in content
    assert 'version:         version.to_s' in content
    assert 'homebrew_prefix: HOMEBREW_PREFIX.to_s' in content
    assert 'brew_path:       (HOMEBREW_PREFIX/"bin/brew").to_s' in content
    assert 'launch_path:     (opt_bin/"falafacil").to_s' in content
    assert 'marker_path:     (opt_prefix/"libexec/falafacil-homebrew.json").to_s' in content
    assert '(libexec/"falafacil-homebrew.json").write JSON.generate(marker_payload)' in content
    assert 'Execute falafacil uma vez após a instalação para registrá-lo no menu de aplicativos.' in content
    assert 'system "#{bin}/falafacil", "--update-probe", version.to_s' in content
    assert "def post_install" not in content
    assert "post_install" not in content
    assert "$HOME" not in content
    assert "~/" not in content

def _load_renderer_module() -> ModuleType:
    import importlib.util

    script_path = ROOT / "scripts" / "render_homebrew_formula.py"
    spec = importlib.util.spec_from_file_location("render_homebrew_formula", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_render_homebrew_formula_success_and_validations(tmp_path: Path) -> None:
    import shutil
    import subprocess

    renderer = _load_renderer_module()
    render_formula = renderer.render_formula
    validate_sha256 = renderer.validate_sha256
    validate_version = renderer.validate_version
    renderer_main = renderer.main

    # Validação de versão
    assert validate_version("0.2.0") == "0.2.0"
    assert validate_version("1.10.5") == "1.10.5"
    for invalid_version in ["v0.2.0", "0.2", "0.2.0-beta", "0.2.0+1", "abc", ""]:
        with pytest.raises(ValueError):
            validate_version(invalid_version)

    # Validação de SHA-256
    dummy_sha = "a" * 64
    assert validate_sha256(dummy_sha) == dummy_sha
    assert validate_sha256(dummy_sha.upper()) == dummy_sha
    for invalid_sha in ["a" * 63, "a" * 65, "g" * 64, "", "not-a-hash"]:
        with pytest.raises(ValueError):
            validate_sha256(invalid_sha)

    # Renderização direta
    template_path = ROOT / "packaging" / "homebrew" / "falafacil.rb.in"
    template_content = template_path.read_text(encoding="utf-8")
    rendered = render_formula(template_content, "0.2.0", dummy_sha)

    assert "@VERSION@" not in rendered
    assert "@SHA256@" not in rendered
    assert "@" not in re.findall(r"@[A-Z0-9_]+@", rendered)
    assert 'url "https://github.com/OthonBreener/falafacil/releases/download/v0.2.0/falafacil-0.2.0-linux-x86_64.tar.gz"' in rendered
    assert 'version "0.2.0"' not in rendered
    assert f'sha256 "{dummy_sha}"' in rendered

    # Renderização via CLI
    output_path = tmp_path / "Formula" / "falafacil.rb"
    exit_code = renderer_main([
        "--version", "0.2.0",
        "--sha256", dummy_sha,
        "--output", str(output_path),
        "--template", str(template_path),
    ])
    assert exit_code == 0
    assert output_path.is_file()
    assert output_path.read_text(encoding="utf-8") == rendered

    # Rejeição CLI em versão inválida
    bad_exit = renderer_main([
        "--version", "v0.2.0",
        "--sha256", dummy_sha,
        "--output", str(tmp_path / "bad.rb"),
    ])
    assert bad_exit != 0

    # Verificação de sintaxe Ruby se disponível
    ruby_bin = shutil.which("ruby")
    brew_bin = shutil.which("brew")
    if ruby_bin is not None:
        syntax_check = subprocess.run(
            [ruby_bin, "-c", str(output_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert syntax_check.returncode == 0, syntax_check.stderr
    elif brew_bin is not None:
        syntax_check = subprocess.run(
            [brew_bin, "ruby", "-e", "RubyVM::InstructionSequence.compile_file(ARGV[0])", str(output_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert syntax_check.returncode == 0, syntax_check.stderr

def test_release_workflow_contract() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "release.yml"
    assert workflow_path.is_file()
    content = workflow_path.read_text(encoding="utf-8")

    # Gatilhos: push de tags v*.*.* e workflow_dispatch com input obrigatório de tag
    assert "tags:" in content
    assert "'v*.*.*'" in content or '"v*.*.*"' in content
    assert "workflow_dispatch:" in content
    assert "inputs:" in content
    assert "tag:" in content
    assert "required: true" in content
    assert "type: string" in content

    # Permissões e runner
    assert "contents: write" in content
    assert "runs-on: ubuntu-24.04" in content

    # Normalização de tag de release no job env para suportar push de tag e dispatch manual em main
    assert "RELEASE_TAG: ${{ inputs.tag || github.ref_name }}" in content

    # Checkouts seguros sem credenciais persistidas
    assert "persist-credentials: false" in content
    assert "token: ${{ secrets.HOMEBREW_TAP_TOKEN }}" not in content, "Checkout do tap deve ser anônimo sem token em step"

    # Dependências do sistema (PortAudio, EGL e PulseAudio para PySide6/QtMultimedia offscreen no runner Ubuntu 24.04)
    assert "sudo apt-get update" in content
    assert "sudo apt-get install -y libportaudio2 libegl1 libpulse0" in content
    assert content.index("libpulse0") < content.index("QT_QPA_PLATFORM=offscreen poetry run pytest -q")
    assert content.index("libegl1") < content.index("QT_QPA_PLATFORM=offscreen poetry run pytest -q")
    assert content.index("libportaudio2") < content.index("QT_QPA_PLATFORM=offscreen poetry run pytest -q")
    # Validação da tag normalizada e versão antes de poetry install, com PYTHONPATH=src
    assert 'TAG="${RELEASE_TAG}"' in content
    assert "^v[0-9]+\\.[0-9]+\\.[0-9]+$" in content
    assert "PYTHONPATH=src python3 -c \"import falafacil; print(falafacil.__version__)\"" in content
    assert 'echo "version=$VERSION" >> "$GITHUB_OUTPUT"' in content
    assert 'echo "tag=$TAG" >> "$GITHUB_OUTPUT"' in content

    # Instalação com split explícito de dependências (incluindo dev e build extras) e pacote raiz
    assert "poetry install --extras dev --extras build\n          poetry run pip install --no-deps -e ." in content
    # Gates de teste e compilação
    assert "QT_QPA_PLATFORM=offscreen poetry run pytest -q" in content
    assert "poetry run python -m compileall -q src tests" in content

    # Build e probe
    assert "./scripts/build_executable.sh" in content
    assert './dist/falafacil --update-probe "${{ steps.version_check.outputs.version }}"' in content

    # Assets de release e permissões 0755
    assert "falafacil-linux-x86_64" in content
    assert "chmod 0755" in content
    assert "tar --owner=0 --group=0 --numeric-owner -czf" in content

    # Invocação do helper de release determinístico com outputs do version_check
    assert "python scripts/publish_release.py" in content
    assert '--tag "${{ steps.version_check.outputs.tag }}"' in content
    assert '--version "${{ steps.version_check.outputs.version }}"' in content
    assert '--asset-raw "${{ steps.package_assets.outputs.asset_raw }}"' in content
    assert '--asset-tar "${{ steps.package_assets.outputs.asset_tar }}"' in content
    assert '--verify-dir "verify_download"' in content
    assert '--github-output "$GITHUB_OUTPUT"' in content

    # Imutabilidade e ausência de flags perigosas ou mascaramento de erros
    assert "--clobber" not in content, "Workflow não deve usar --clobber"
    assert "|| true" not in content, "Workflow não deve conter || true para mascarar falhas"

    # SHA da fórmula derivado do output do step de release
    assert "steps.release_assets.outputs.sha256" in content

    # Sincronização do tap isolada com ephemeral askpass
    assert "repository: 'OthonBreener/homebrew-falafacil'" in content
    assert "path: 'homebrew-tap'" in content
    assert "HOMEBREW_TAP_TOKEN: ${{ secrets.HOMEBREW_TAP_TOKEN }}" in content
    assert "GIT_ASKPASS" in content
    assert "x-access-token@github.com" in content
    assert "rm -f \"$ASKPASS_SH\"" in content
    assert "scripts/render_homebrew_formula.py" in content
    assert "Homebrew/actions/setup-homebrew@8f3d1ec8a696b3b9d9a6c3696b6c73033cab69e4" in content

    # Criação de tap temporário local e auditoria/instalação/teste por nome lógico exato
    assert "brew tap-new --no-git OthonBreener/falafacil" in content
    assert 'mkdir -p "$(brew --repo OthonBreener/falafacil)/Formula"' in content
    assert 'cp homebrew-tap/Formula/falafacil.rb "$(brew --repo OthonBreener/falafacil)/Formula/falafacil.rb"' in content
    assert "brew audit --formula OthonBreener/falafacil/falafacil" in content
    assert "brew install --build-from-source OthonBreener/falafacil/falafacil" in content
    assert "brew test OthonBreener/falafacil/falafacil" in content

    # Rejeição estrita de comandos brew por caminho de arquivo (rejeitados no Homebrew 6)
    assert not re.search(r"brew\s+(?:audit|install|test)\s+.*homebrew-tap", content)
    assert not re.search(r"brew\s+(?:audit|install|test)\s+.*\.rb", content)


def test_release_workflow_tag_validation_simulation(tmp_path: Path) -> None:
    import subprocess

    workflow_path = ROOT / ".github" / "workflows" / "release.yml"
    content = workflow_path.read_text(encoding="utf-8")

    # Extrai o script de validação de versão do step version_check
    assert 'TAG="${RELEASE_TAG}"' in content

    bash_script = """
    set -euo pipefail
    TAG="${RELEASE_TAG}"
    if ! echo "$TAG" | grep -Eq '^v[0-9]+\\.[0-9]+\\.[0-9]+$'; then
      echo "Tag '$TAG' não é um SemVer válido no formato vX.Y.Z" >&2
      exit 1
    fi
    VERSION="${TAG#v}"
    PACKAGE_VERSION=$(PYTHONPATH=src python3 -c "import falafacil; print(falafacil.__version__)")
    if [ "$VERSION" != "$PACKAGE_VERSION" ]; then
      echo "Versão da tag '$VERSION' diverge de falafacil.__version__ '$PACKAGE_VERSION'" >&2
      exit 1
    fi
    echo "version=$VERSION" >> "$GITHUB_OUTPUT"
    echo "tag=$TAG" >> "$GITHUB_OUTPUT"
    """

    output_file = tmp_path / "github_output.txt"

    # 1. Caso válido correspondente à versão do pacote (v0.2.1)
    output_file.write_text("", encoding="utf-8")
    env = os.environ.copy()
    env["RELEASE_TAG"] = "v0.2.1"
    env["GITHUB_OUTPUT"] = str(output_file)
    env["PYTHONPATH"] = str(ROOT / "src")
    res = subprocess.run(
        ["bash", "-c", bash_script],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    out_text = output_file.read_text(encoding="utf-8")
    assert "version=0.2.1\n" in out_text
    assert "tag=v0.2.1\n" in out_text

    # 2. Caso com divergência de versão (v0.3.0)
    env["RELEASE_TAG"] = "v0.3.0"
    res = subprocess.run(
        ["bash", "-c", bash_script],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 1
    assert "diverge de falafacil.__version__" in res.stderr

    # 3. Caso de branch name em vez de tag SemVer (ex: main)
    env["RELEASE_TAG"] = "main"
    res = subprocess.run(
        ["bash", "-c", bash_script],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 1
    assert "não é um SemVer válido" in res.stderr

    # 4. Caso sem prefixo 'v' (ex: 0.2.0)
    env["RELEASE_TAG"] = "0.2.0"
    res = subprocess.run(
        ["bash", "-c", bash_script],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 1
    assert "não é um SemVer válido" in res.stderr

    # 5. Caso inválido SemVer (ex: v0.2.0-beta)
    env["RELEASE_TAG"] = "v0.2.0-beta"
    res = subprocess.run(
        ["bash", "-c", bash_script],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 1
    assert "não é um SemVer válido" in res.stderr

    # 6. Caso vazio
    env["RELEASE_TAG"] = ""
    res = subprocess.run(
        ["bash", "-c", bash_script],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 1
    assert "não é um SemVer válido" in res.stderr


def test_tar_asset_structure_assumptions(tmp_path: Path) -> None:
    import tarfile
    # Cria binário sintético
    build_dir = tmp_path / "tar_root"
    build_dir.mkdir()
    exe_file = build_dir / "falafacil"
    exe_file.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe_file.chmod(0o755)

    # Empacota em tar.gz com raiz direta
    tar_path = tmp_path / "falafacil-0.2.0-linux-x86_64.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(exe_file, arcname="falafacil")

    # Valida estrutura do arquivo
    with tarfile.open(tar_path, "r:gz") as tar:
        members = tar.getmembers()
        assert len(members) == 1
        member = members[0]
        assert member.name == "falafacil"
        assert member.isfile()
        assert member.mode == 0o755

def _load_publish_release_module() -> ModuleType:
    import importlib.util

    script_path = ROOT / "scripts" / "publish_release.py"
    spec = importlib.util.spec_from_file_location("publish_release", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["publish_release"] = module
    spec.loader.exec_module(module)
    return module

def _create_synthetic_release_assets(
    tmp_path: Path,
    version: str = "0.2.0",
    *,
    marker: str = "default",
    coherent: bool = True,
    executable_mode: int = 0o755,
    tar_member_name: str = "falafacil",
    extra_members: bool = False,
) -> tuple[Path, Path]:
    import tarfile

    assets_dir = tmp_path / f"assets_{marker}"
    assets_dir.mkdir(parents=True, exist_ok=True)

    raw_path = assets_dir / "falafacil-linux-x86_64"
    raw_content = (
        f"#!/bin/sh\n# marker={marker}\n"
        f"if [ \"$1\" = \"--update-probe\" ] && [ \"$2\" = \"{version}\" ]; then exit 0; else exit 1; fi\n"
    ).encode("utf-8")
    raw_path.write_bytes(raw_content)
    raw_path.chmod(0o755)

    tar_path = assets_dir / f"falafacil-{version}-linux-x86_64.tar.gz"
    tar_root = tmp_path / f"tar_root_{marker}"
    tar_root.mkdir(parents=True, exist_ok=True)
    tar_exe = tar_root / tar_member_name
    if coherent:
        tar_exe.write_bytes(raw_content)
    else:
        tar_exe.write_bytes(f"#!/bin/sh\n# divergent marker={marker}\nexit 0\n".encode("utf-8"))
    tar_exe.chmod(executable_mode)

    with tarfile.open(tar_path, "w:gz", format=tarfile.PAX_FORMAT) as tar:
        tarinfo = tar.gettarinfo(str(tar_exe), arcname=tar_member_name)
        tarinfo.mode = executable_mode
        tarinfo.uid = 0
        tarinfo.gid = 0
        tarinfo.uname = ""
        tarinfo.gname = ""
        with tar_exe.open("rb") as fp:
            tar.addfile(tarinfo, fp)
        if extra_members:
            extra_file = tar_root / "extra.txt"
            extra_file.write_text("extra", encoding="utf-8")
            extra_info = tar.gettarinfo(str(extra_file), arcname="extra.txt")
            with extra_file.open("rb") as fp:
                tar.addfile(extra_info, fp)

    return raw_path, tar_path


class FakeGhRunner:
    def __init__(
        self,
        *,
        exists: bool = False,
        is_draft: bool = False,
        remote_assets: dict[str, bytes] | None = None,
        view_error: str | None = None,
        view_payload: Any = None,
        create_error: str | None = None,
        upload_error: str | None = None,
        download_error: str | None = None,
        edit_error: str | None = None,
        probe_returncode: int = 0,
    ) -> None:
        self.exists = exists
        self.is_draft = is_draft
        self.remote_assets = dict(remote_assets or {})
        self.view_error = view_error
        self.view_payload = view_payload
        self.create_error = create_error
        self.upload_error = upload_error
        self.download_error = download_error
        self.edit_error = edit_error
        self.probe_returncode = probe_returncode
        self.calls: list[list[str]] = []

    def __call__(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        import json

        cmd = [str(a) for a in args]
        self.calls.append(cmd)

        if len(cmd) >= 2 and cmd[1] == "--update-probe":
            if self.probe_returncode != 0:
                return subprocess.CompletedProcess(
                    cmd, returncode=self.probe_returncode, stdout="", stderr="probe failure"
                )
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="probe ok", stderr="")

        if not cmd or cmd[0] != "gh":
            return subprocess.run(cmd, text=True, capture_output=capture_output, check=check)

        subcmd = cmd[1] if len(cmd) > 1 else ""
        if subcmd == "release":
            action = cmd[2] if len(cmd) > 2 else ""
            if action == "view":
                if self.view_error is not None:
                    return subprocess.CompletedProcess(
                        cmd, returncode=1, stdout="", stderr=self.view_error
                    )
                if not self.exists:
                    return subprocess.CompletedProcess(
                        cmd, returncode=1, stdout="", stderr="release not found"
                    )
                if self.view_payload is not None:
                    stdout = (
                        self.view_payload
                        if isinstance(self.view_payload, str)
                        else json.dumps(self.view_payload)
                    )
                    return subprocess.CompletedProcess(
                        cmd, returncode=0, stdout=stdout, stderr=""
                    )
                payload = {
                    "tagName": cmd[3],
                    "isDraft": self.is_draft,
                    "assets": [{"name": k} for k in sorted(self.remote_assets.keys())],
                }
                return subprocess.CompletedProcess(
                    cmd, returncode=0, stdout=json.dumps(payload), stderr=""
                )
            elif action == "create":
                if self.create_error:
                    return subprocess.CompletedProcess(
                        cmd, returncode=1, stdout="", stderr=self.create_error
                    )
                if self.exists:
                    return subprocess.CompletedProcess(
                        cmd, returncode=1, stdout="", stderr="release already exists"
                    )
                self.exists = True
                self.is_draft = "--draft" in cmd
                return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

            elif action == "upload":
                if self.upload_error:
                    return subprocess.CompletedProcess(
                        cmd, returncode=1, stdout="", stderr=self.upload_error
                    )
                if not self.exists:
                    return subprocess.CompletedProcess(
                        cmd, returncode=1, stdout="", stderr="release not found"
                    )
                files = [a for a in cmd[4:] if not a.startswith("-")]
                for f_str in files:
                    p = Path(f_str)
                    if p.name in self.remote_assets and "--clobber" not in cmd:
                        return subprocess.CompletedProcess(
                            cmd,
                            returncode=1,
                            stdout="",
                            stderr=f"asset {p.name} already exists (duplicate upload rejected)",
                        )
                    self.remote_assets[p.name] = p.read_bytes()
                return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

            elif action == "download":
                if self.download_error:
                    return subprocess.CompletedProcess(
                        cmd, returncode=1, stdout="", stderr=self.download_error
                    )
                if not self.exists:
                    return subprocess.CompletedProcess(
                        cmd, returncode=1, stdout="", stderr="release not found"
                    )
                dir_idx = cmd.index("--dir")
                dest_dir = Path(cmd[dir_idx + 1])
                dest_dir.mkdir(parents=True, exist_ok=True)
                for name, data in self.remote_assets.items():
                    (dest_dir / name).write_bytes(data)
                return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

            elif action == "edit":
                if self.edit_error:
                    return subprocess.CompletedProcess(
                        cmd, returncode=1, stdout="", stderr=self.edit_error
                    )
                if not self.exists:
                    return subprocess.CompletedProcess(
                        cmd, returncode=1, stdout="", stderr="release not found"
                    )
                if "--draft=false" in cmd:
                    self.is_draft = False
                return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        return subprocess.CompletedProcess(
            cmd, returncode=1, stdout="", stderr="unsupported gh command"
        )


def test_publish_release_state_new(tmp_path: Path) -> None:
    mod = _load_publish_release_module()
    raw_path, tar_path = _create_synthetic_release_assets(tmp_path, "0.2.0", marker="local")
    verify_dir = tmp_path / "verify_download"
    github_output = tmp_path / "github_output.txt"

    runner = FakeGhRunner(exists=False)

    sha256 = mod.publish_or_verify_release(
        tag="v0.2.0",
        version="0.2.0",
        asset_raw=raw_path,
        asset_tar=tar_path,
        verify_dir=verify_dir,
        github_output=github_output,
        runner=runner,
    )

    assert len(sha256) == 64
    assert runner.exists is True
    assert runner.is_draft is False
    assert "falafacil-linux-x86_64" in runner.remote_assets
    assert "falafacil-0.2.0-linux-x86_64.tar.gz" in runner.remote_assets

    # Confirma sequência exata: view -> create -> upload -> download -> probe -> edit
    call_signatures = [
        (c[1], c[2]) for c in runner.calls if len(c) > 2 and c[0] == "gh" and c[1] == "release"
    ]
    assert call_signatures == [
        ("release", "view"),
        ("release", "create"),
        ("release", "upload"),
        ("release", "download"),
        ("release", "edit"),
    ]
    assert any(c[1] == "--update-probe" for c in runner.calls)
    assert f"sha256={sha256}\n" in github_output.read_text(encoding="utf-8")


def test_publish_release_state_draft_empty(tmp_path: Path) -> None:
    mod = _load_publish_release_module()
    raw_path, tar_path = _create_synthetic_release_assets(tmp_path, "0.2.0", marker="local")
    verify_dir = tmp_path / "verify_download"

    runner = FakeGhRunner(exists=True, is_draft=True, remote_assets={})

    sha256 = mod.publish_or_verify_release(
        tag="v0.2.0",
        version="0.2.0",
        asset_raw=raw_path,
        asset_tar=tar_path,
        verify_dir=verify_dir,
        runner=runner,
    )

    assert len(sha256) == 64
    assert runner.is_draft is False
    assert "falafacil-linux-x86_64" in runner.remote_assets
    assert "falafacil-0.2.0-linux-x86_64.tar.gz" in runner.remote_assets

    upload_call = next(c for c in runner.calls if len(c) > 2 and c[2] == "upload")
    assert str(raw_path) in upload_call
    assert str(tar_path) in upload_call


def test_publish_release_state_draft_raw_only_derives_tar_from_remote_authority(
    tmp_path: Path,
) -> None:
    mod = _load_publish_release_module()
    raw_local, tar_local = _create_synthetic_release_assets(tmp_path, "0.2.0", marker="local")
    raw_remote, _ = _create_synthetic_release_assets(tmp_path, "0.2.0", marker="remote_prior")
    verify_dir = tmp_path / "verify_download"

    # Garante que os assets remotos e locais divergem byte a byte
    assert raw_local.read_bytes() != raw_remote.read_bytes()

    runner = FakeGhRunner(
        exists=True,
        is_draft=True,
        remote_assets={"falafacil-linux-x86_64": raw_remote.read_bytes()},
    )

    sha256 = mod.publish_or_verify_release(
        tag="v0.2.0",
        version="0.2.0",
        asset_raw=raw_local,
        asset_tar=tar_local,
        verify_dir=verify_dir,
        runner=runner,
    )

    assert len(sha256) == 64
    assert runner.is_draft is False
    assert "falafacil-0.2.0-linux-x86_64.tar.gz" in runner.remote_assets

    # Upload deve enviar SOMENTE o tarball derivado a partir do raw remoto, sem o local
    upload_call = next(c for c in runner.calls if len(c) > 2 and c[2] == "upload")
    assert str(tar_local) not in upload_call
    assert str(raw_local) not in upload_call
    assert "falafacil-0.2.0-linux-x86_64.tar.gz" in upload_call[-1]

    # SHA256 retornado corresponde ao tarball derivado do raw remoto
    uploaded_tar_bytes = runner.remote_assets["falafacil-0.2.0-linux-x86_64.tar.gz"]
    hasher = hashlib.sha256(uploaded_tar_bytes)
    assert sha256 == hasher.hexdigest()


def test_publish_release_state_draft_tar_only_derives_raw_from_remote_authority(
    tmp_path: Path,
) -> None:
    mod = _load_publish_release_module()
    raw_local, tar_local = _create_synthetic_release_assets(tmp_path, "0.2.0", marker="local")
    _, tar_remote = _create_synthetic_release_assets(tmp_path, "0.2.0", marker="remote_prior")
    verify_dir = tmp_path / "verify_download"

    # Garante que os assets remotos e locais divergem byte a byte
    assert tar_local.read_bytes() != tar_remote.read_bytes()

    runner = FakeGhRunner(
        exists=True,
        is_draft=True,
        remote_assets={"falafacil-0.2.0-linux-x86_64.tar.gz": tar_remote.read_bytes()},
    )

    sha256 = mod.publish_or_verify_release(
        tag="v0.2.0",
        version="0.2.0",
        asset_raw=raw_local,
        asset_tar=tar_local,
        verify_dir=verify_dir,
        runner=runner,
    )

    assert len(sha256) == 64
    assert runner.is_draft is False
    assert "falafacil-linux-x86_64" in runner.remote_assets

    # Upload deve enviar SOMENTE o binário raw extraído do tar remoto, sem o local
    upload_call = next(c for c in runner.calls if len(c) > 2 and c[2] == "upload")
    assert str(raw_local) not in upload_call
    assert str(tar_local) not in upload_call
    assert "falafacil-linux-x86_64" in upload_call[-1]

    # SHA256 retornado corresponde ao tar remoto existente
    assert sha256 == mod.compute_sha256(tar_remote)


def test_publish_release_state_draft_complete_uses_remote_authority_without_upload(
    tmp_path: Path,
) -> None:
    mod = _load_publish_release_module()
    raw_local, tar_local = _create_synthetic_release_assets(tmp_path, "0.2.0", marker="local")
    raw_remote, tar_remote = _create_synthetic_release_assets(tmp_path, "0.2.0", marker="remote_prior")
    verify_dir = tmp_path / "verify_download"

    # Assets remotos divergem byte a byte do rebuild local mas são autoritativos
    assert raw_local.read_bytes() != raw_remote.read_bytes()
    assert tar_local.read_bytes() != tar_remote.read_bytes()

    runner = FakeGhRunner(
        exists=True,
        is_draft=True,
        remote_assets={
            "falafacil-linux-x86_64": raw_remote.read_bytes(),
            "falafacil-0.2.0-linux-x86_64.tar.gz": tar_remote.read_bytes(),
        },
    )

    sha256 = mod.publish_or_verify_release(
        tag="v0.2.0",
        version="0.2.0",
        asset_raw=raw_local,
        asset_tar=tar_local,
        verify_dir=verify_dir,
        runner=runner,
    )

    assert len(sha256) == 64
    assert runner.is_draft is False

    # Nenhum upload deve ter sido executado
    assert not any(len(c) > 2 and c[2] == "upload" for c in runner.calls)
    # Edit para publicar deve ter sido chamado após download e verificação
    assert any(len(c) > 2 and c[2] == "edit" for c in runner.calls)
    assert sha256 == mod.compute_sha256(tar_remote)


def test_publish_release_state_published_is_strict_readonly_with_remote_authority(
    tmp_path: Path,
) -> None:
    mod = _load_publish_release_module()
    raw_local, tar_local = _create_synthetic_release_assets(tmp_path, "0.2.0", marker="local")
    raw_remote, tar_remote = _create_synthetic_release_assets(tmp_path, "0.2.0", marker="remote_prior")
    verify_dir = tmp_path / "verify_download"

    # Release já publicada com assets imutáveis de execução anterior
    assert raw_local.read_bytes() != raw_remote.read_bytes()
    assert tar_local.read_bytes() != tar_remote.read_bytes()

    runner = FakeGhRunner(
        exists=True,
        is_draft=False,
        remote_assets={
            "falafacil-linux-x86_64": raw_remote.read_bytes(),
            "falafacil-0.2.0-linux-x86_64.tar.gz": tar_remote.read_bytes(),
        },
    )

    sha256 = mod.publish_or_verify_release(
        tag="v0.2.0",
        version="0.2.0",
        asset_raw=raw_local,
        asset_tar=tar_local,
        verify_dir=verify_dir,
        runner=runner,
    )

    assert len(sha256) == 64
    # Nenhuma mutação: create, upload ou edit são proibidos em release publicada
    assert not any(len(c) > 2 and c[2] in {"create", "upload", "edit"} for c in runner.calls)
    # Apenas view e download permitidos
    gh_actions = [c[2] for c in runner.calls if len(c) > 2 and c[0] == "gh"]
    assert gh_actions == ["view", "download"]
    assert sha256 == mod.compute_sha256(tar_remote)


def test_publish_release_published_missing_asset_fails_closed(tmp_path: Path) -> None:
    mod = _load_publish_release_module()
    raw_local, tar_local = _create_synthetic_release_assets(tmp_path, "0.2.0", marker="local")
    verify_dir = tmp_path / "verify_download"

    # Publicada sem o tarball -> fail-closed
    runner_no_tar = FakeGhRunner(
        exists=True,
        is_draft=False,
        remote_assets={"falafacil-linux-x86_64": raw_local.read_bytes()},
    )
    with pytest.raises(mod.ReleaseError, match="não contém ambos os assets esperados"):
        mod.publish_or_verify_release(
            tag="v0.2.0",
            version="0.2.0",
            asset_raw=raw_local,
            asset_tar=tar_local,
            verify_dir=verify_dir,
            runner=runner_no_tar,
        )

    # Publicada sem o raw -> fail-closed
    runner_no_raw = FakeGhRunner(
        exists=True,
        is_draft=False,
        remote_assets={"falafacil-0.2.0-linux-x86_64.tar.gz": tar_local.read_bytes()},
    )
    with pytest.raises(mod.ReleaseError, match="não contém ambos os assets esperados"):
        mod.publish_or_verify_release(
            tag="v0.2.0",
            version="0.2.0",
            asset_raw=raw_local,
            asset_tar=tar_local,
            verify_dir=verify_dir,
            runner=runner_no_raw,
        )


@pytest.mark.parametrize(
    "failure_kwargs,match_pattern",
    [
        ({"view_error": ""}, "Falha ao consultar release"),
        ({"view_error": "Could not resolve host: github.com"}, "Falha ao consultar release"),
        ({"view_error": "HTTP 404: Not Found (https://api.github.com/repos/owner/falafacil)"}, "Falha ao consultar release"),
        ({"view_error": "repository not found"}, "Falha ao consultar release"),
        ({"view_error": "404 Not Found"}, "Falha ao consultar release"),
        ({"view_error": "HTTP 500 internal server error"}, "Falha ao consultar release"),
        ({"view_error": "HTTP 502 Bad Gateway: release not found"}, "Falha ao consultar release"),
        ({"view_error": "error: failed with 404: release not found in downstream service"}, "Falha ao consultar release"),
        ({"view_error": "authentication required"}, "Falha ao consultar release"),
        ({"create_error": "rate limit exceeded"}, "Falha ao criar draft release"),
        ({"upload_error": "connection reset by peer"}, "Falha ao fazer upload dos assets"),
        ({"download_error": "download timeout"}, "Falha no download dos assets"),
        ({"edit_error": "permission denied"}, "Falha ao publicar release"),
        ({"probe_returncode": 1}, "Probe do executável falhou"),
    ],
)
def test_publish_release_injected_failures_fail_closed(
    tmp_path: Path, failure_kwargs: dict[str, Any], match_pattern: str
) -> None:
    mod = _load_publish_release_module()
    raw_local, tar_local = _create_synthetic_release_assets(tmp_path, "0.2.0", marker="local")
    verify_dir = tmp_path / "verify_download"
    runner = FakeGhRunner(exists=False, **failure_kwargs)

    with pytest.raises(mod.ReleaseError, match=match_pattern):
        mod.publish_or_verify_release(
            tag="v0.2.0",
            version="0.2.0",
            asset_raw=raw_local,
            asset_tar=tar_local,
            verify_dir=verify_dir,
            runner=runner,
        )

    if "view_error" in failure_kwargs:
        assert not any(
            len(c) > 2 and c[2] in {"create", "upload", "edit", "download"} for c in runner.calls
        )


def test_publish_release_raw_tar_incoherence_fails_before_publish(tmp_path: Path) -> None:
    mod = _load_publish_release_module()
    raw_remote, incoherent_tar_remote = _create_synthetic_release_assets(
        tmp_path, "0.2.0", coherent=False, marker="incoherent"
    )
    raw_local, tar_local = _create_synthetic_release_assets(tmp_path, "0.2.0", marker="local")
    verify_dir = tmp_path / "verify_download"

    runner = FakeGhRunner(
        exists=True,
        is_draft=True,
        remote_assets={
            "falafacil-linux-x86_64": raw_remote.read_bytes(),
            "falafacil-0.2.0-linux-x86_64.tar.gz": incoherent_tar_remote.read_bytes(),
        },
    )

    with pytest.raises(mod.ReleaseError, match="Incoerência raw↔tar"):
        mod.publish_or_verify_release(
            tag="v0.2.0",
            version="0.2.0",
            asset_raw=raw_local,
            asset_tar=tar_local,
            verify_dir=verify_dir,
            runner=runner,
        )

    # Confirma que a publicação JAMAIS ocorre antes da verificação passar
    assert not any(len(c) > 2 and c[2] == "edit" for c in runner.calls)


def test_publish_release_tar_structure_validations(tmp_path: Path) -> None:
    mod = _load_publish_release_module()
    raw_local, tar_local = _create_synthetic_release_assets(tmp_path, "0.2.0", marker="local")
    verify_dir = tmp_path / "verify_download"

    # Tar com nome de membro incorreto
    raw_remote, wrong_name_tar = _create_synthetic_release_assets(
        tmp_path, "0.2.0", tar_member_name="wrong_name", marker="wrong_name"
    )
    runner_wrong_name = FakeGhRunner(
        exists=True,
        is_draft=True,
        remote_assets={
            "falafacil-linux-x86_64": raw_remote.read_bytes(),
            "falafacil-0.2.0-linux-x86_64.tar.gz": wrong_name_tar.read_bytes(),
        },
    )
    with pytest.raises(mod.ReleaseError, match="deve conter o arquivo 'falafacil' na raiz"):
        mod.publish_or_verify_release(
            tag="v0.2.0",
            version="0.2.0",
            asset_raw=raw_local,
            asset_tar=tar_local,
            verify_dir=verify_dir,
            runner=runner_wrong_name,
        )

    # Tar com múltiplos membros
    raw_remote, multi_tar = _create_synthetic_release_assets(
        tmp_path, "0.2.0", extra_members=True, marker="multi"
    )
    runner_multi = FakeGhRunner(
        exists=True,
        is_draft=True,
        remote_assets={
            "falafacil-linux-x86_64": raw_remote.read_bytes(),
            "falafacil-0.2.0-linux-x86_64.tar.gz": multi_tar.read_bytes(),
        },
    )
    with pytest.raises(mod.ReleaseError, match="deve conter exatamente 1 arquivo na raiz"):
        mod.publish_or_verify_release(
            tag="v0.2.0",
            version="0.2.0",
            asset_raw=raw_local,
            asset_tar=tar_local,
            verify_dir=verify_dir,
            runner=runner_multi,
        )


@pytest.mark.parametrize(
    "unsafe_mode",
    [
        0o644,
        0o754,
        0o777,
        0o4755,
        0o2755,
        0o1755,
    ],
    ids=["0644", "0754", "0777", "setuid-04755", "setgid-02755", "sticky-01755"],
)
def test_publish_release_tar_mode_validations(tmp_path: Path, unsafe_mode: int) -> None:
    mod = _load_publish_release_module()
    raw_local, tar_local = _create_synthetic_release_assets(tmp_path, "0.2.0", marker="local")
    verify_dir = tmp_path / "verify_download"

    raw_remote, unsafe_tar = _create_synthetic_release_assets(
        tmp_path, "0.2.0", executable_mode=unsafe_mode, marker=f"mode_{oct(unsafe_mode)}"
    )
    runner = FakeGhRunner(
        exists=True,
        is_draft=True,
        remote_assets={
            "falafacil-linux-x86_64": raw_remote.read_bytes(),
            "falafacil-0.2.0-linux-x86_64.tar.gz": unsafe_tar.read_bytes(),
        },
    )
    with pytest.raises(mod.ReleaseError, match="deve ter permissão exata 0755"):
        mod.publish_or_verify_release(
            tag="v0.2.0",
            version="0.2.0",
            asset_raw=raw_local,
            asset_tar=tar_local,
            verify_dir=verify_dir,
            runner=runner,
        )

    # Nenhuma publicação deve ocorrer
    assert not any(len(c) > 2 and c[2] == "edit" for c in runner.calls)


@pytest.mark.parametrize(
    "payload,match_pattern",
    [
        ([], "deve ser um objeto JSON"),
        ("not json", "Resposta JSON inválida"),
        (123, "deve ser um objeto JSON"),
        ({}, "não contém 'tagName' válido"),
        ({"tagName": ""}, "não contém 'tagName' válido"),
        ({"tagName": "v0.1.0"}, "divergente da tag solicitada"),
        ({"tagName": "v0.2.0"}, "campo 'isDraft' deve ser booleano"),
        ({"tagName": "v0.2.0", "isDraft": "false"}, "campo 'isDraft' deve ser booleano"),
        ({"tagName": "v0.2.0", "isDraft": 0}, "campo 'isDraft' deve ser booleano"),
        ({"tagName": "v0.2.0", "isDraft": False}, "campo 'assets' deve ser uma lista"),
        ({"tagName": "v0.2.0", "isDraft": False, "assets": "invalid"}, "campo 'assets' deve ser uma lista"),
        ({"tagName": "v0.2.0", "isDraft": False, "assets": ["not a dict"]}, "não é um objeto"),
        ({"tagName": "v0.2.0", "isDraft": False, "assets": [{}]}, "não possui 'name' válido"),
        ({"tagName": "v0.2.0", "isDraft": False, "assets": [{"name": ""}]}, "não possui 'name' válido"),
        ({"tagName": "v0.2.0", "isDraft": False, "assets": [{"name": "   "}]}, "não possui 'name' válido"),
        (
            {"tagName": "v0.2.0", "isDraft": False, "assets": [{"name": "falafacil-linux-x86_64"}, {"name": "falafacil-linux-x86_64"}]},
            "Asset duplicado",
        ),
    ],
)
def test_publish_release_view_schema_validations(
    tmp_path: Path, payload: Any, match_pattern: str
) -> None:
    mod = _load_publish_release_module()
    raw_local, tar_local = _create_synthetic_release_assets(tmp_path, "0.2.0", marker="local")
    verify_dir = tmp_path / "verify_download"
    runner = FakeGhRunner(exists=True, view_payload=payload)

    with pytest.raises(mod.ReleaseError, match=match_pattern):
        mod.publish_or_verify_release(
            tag="v0.2.0",
            version="0.2.0",
            asset_raw=raw_local,
            asset_tar=tar_local,
            verify_dir=verify_dir,
            runner=runner,
        )

    # Nenhuma mutação deve ter sido realizada
    assert not any(len(c) > 2 and c[2] in {"create", "upload", "edit"} for c in runner.calls)


@pytest.mark.parametrize(
    "not_found_msg",
    [
        "release not found",
        "Release not found",
        "error: release not found",
        "release 'v0.2.0' not found",
        'release "v0.2.0" not found',
        "release v0.2.0 not found",
        "GraphQL: Could not resolve to a Release with the tag 'v0.2.0'",
        'GraphQL: Could not resolve to a Release with the tag "v0.2.0"',
    ],
)
def test_publish_release_unequivocal_not_found_transitions_to_new(
    tmp_path: Path, not_found_msg: str
) -> None:
    mod = _load_publish_release_module()
    raw_local, tar_local = _create_synthetic_release_assets(tmp_path, "0.2.0", marker="local")
    verify_dir = tmp_path / "verify_download"
    runner = FakeGhRunner(exists=False, view_error=not_found_msg)

    sha256 = mod.publish_or_verify_release(
        tag="v0.2.0",
        version="0.2.0",
        asset_raw=raw_local,
        asset_tar=tar_local,
        verify_dir=verify_dir,
        runner=runner,
    )
    assert len(sha256) == 64
    assert runner.exists is True
    assert runner.is_draft is False


def test_publish_release_validations_and_cli(tmp_path: Path) -> None:
    mod = _load_publish_release_module()

    # Validações de tag e versão
    tag, version = mod.validate_tag_and_version("v0.2.0", "0.2.0")
    assert tag == "v0.2.0"
    assert version == "0.2.0"

    with pytest.raises(mod.ReleaseError, match="Tag inválida"):
        mod.validate_tag_and_version("0.2.0", "0.2.0")

    with pytest.raises(mod.ReleaseError, match="Versão inválida"):
        mod.validate_tag_and_version("v0.2.0", "v0.2.0")

    with pytest.raises(mod.ReleaseError, match="diverge"):
        mod.validate_tag_and_version("v0.2.0", "0.2.1")

    # Arquivos locais ausentes ou com nomes inválidos
    raw_path, tar_path = _create_synthetic_release_assets(tmp_path, "0.2.0", marker="valid")
    wrong_raw = tmp_path / "wrong-raw"
    wrong_raw.write_bytes(b"content")
    wrong_tar = tmp_path / "wrong-tar.tar.gz"
    wrong_tar.write_bytes(b"content")

    with pytest.raises(mod.ReleaseError, match="diverge do esperado"):
        mod.publish_or_verify_release(
            tag="v0.2.0",
            version="0.2.0",
            asset_raw=wrong_raw,
            asset_tar=tar_path,
            verify_dir=tmp_path / "verify",
            runner=FakeGhRunner(exists=False),
        )

    with pytest.raises(mod.ReleaseError, match="diverge do esperado"):
        mod.publish_or_verify_release(
            tag="v0.2.0",
            version="0.2.0",
            asset_raw=raw_path,
            asset_tar=wrong_tar,
            verify_dir=tmp_path / "verify",
            runner=FakeGhRunner(exists=False),
        )

    # CLI main retorna 1 em erro
    exit_code = mod.main(["--tag", "invalid", "--version", "0.2.0", "--asset-raw", "none", "--asset-tar", "none"])
    assert exit_code == 1


def test_publish_release_with_real_subprocess_fake_gh(tmp_path: Path) -> None:
    import json

    mod = _load_publish_release_module()
    raw_local, tar_local = _create_synthetic_release_assets(tmp_path, "0.2.0", marker="local")
    verify_dir = tmp_path / "verify_download"
    github_output = tmp_path / "github_output.txt"

    state_file = tmp_path / "gh_state.json"
    state_file.write_text(
        json.dumps({"exists": False, "is_draft": False, "assets": {}}), encoding="utf-8"
    )

    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        f"""#!/usr/bin/env python3
import sys, json
from pathlib import Path

state_file = Path("{state_file}")
state = json.loads(state_file.read_text(encoding="utf-8"))

args = sys.argv[1:]
if not args or args[0] != "release":
    sys.exit(1)

action = args[1]
if action == "view":
    if not state["exists"]:
        sys.stderr.write("release not found\\n")
        sys.exit(1)
    tag = args[2]
    out = {{
        "tagName": tag,
        "isDraft": state["is_draft"],
        "assets": [{{"name": k}} for k in sorted(state["assets"].keys())]
    }}
    sys.stdout.write(json.dumps(out))
    sys.exit(0)
elif action == "create":
    tag = args[2]
    if state["exists"]:
        sys.stderr.write("already exists\\n")
        sys.exit(1)
    state["exists"] = True
    state["is_draft"] = "--draft" in args
    state_file.write_text(json.dumps(state), encoding="utf-8")
    sys.exit(0)
elif action == "upload":
    tag = args[2]
    files = [a for a in args[3:] if not a.startswith("-")]
    for f in files:
        p = Path(f)
        if p.name in state["assets"] and "--clobber" not in args:
            sys.stderr.write(f"duplicate asset {{p.name}}\\n")
            sys.exit(1)
        state["assets"][p.name] = p.read_bytes().hex()
    state_file.write_text(json.dumps(state), encoding="utf-8")
    sys.exit(0)
elif action == "download":
    tag = args[2]
    dir_idx = args.index("--dir")
    dest_dir = Path(args[dir_idx + 1])
    dest_dir.mkdir(parents=True, exist_ok=True)
    for name, hex_content in state["assets"].items():
        (dest_dir / name).write_bytes(bytes.fromhex(hex_content))
    sys.exit(0)
elif action == "edit":
    if "--draft=false" in args:
        state["is_draft"] = False
    state_file.write_text(json.dumps(state), encoding="utf-8")
    sys.exit(0)

sys.exit(1)
""",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)

    # 1. Execução inicial: NEW release
    sha256 = mod.publish_or_verify_release(
        tag="v0.2.0",
        version="0.2.0",
        asset_raw=raw_local,
        asset_tar=tar_local,
        verify_dir=verify_dir,
        github_output=github_output,
        gh_cmd=str(fake_gh),
    )

    assert len(sha256) == 64
    final_state = json.loads(state_file.read_text(encoding="utf-8"))
    assert final_state["exists"] is True
    assert final_state["is_draft"] is False
    assert "falafacil-0.2.0-linux-x86_64.tar.gz" in final_state["assets"]
    assert f"sha256={sha256}\n" in github_output.read_text(encoding="utf-8")

    # 2. Reexecução (retry após release publicada) com novo rebuild local byte a byte diferente
    raw_rebuild, tar_rebuild = _create_synthetic_release_assets(
        tmp_path, "0.2.0", marker="local_rebuild_different"
    )
    assert raw_rebuild.read_bytes() != raw_local.read_bytes()
    assert tar_rebuild.read_bytes() != tar_local.read_bytes()

    github_output_retry = tmp_path / "github_output_retry.txt"
    sha256_retry = mod.publish_or_verify_release(
        tag="v0.2.0",
        version="0.2.0",
        asset_raw=raw_rebuild,
        asset_tar=tar_rebuild,
        verify_dir=verify_dir,
        github_output=github_output_retry,
        gh_cmd=str(fake_gh),
    )

    # O hash retornado deve ser estritamente o do asset remoto já publicado e imutável
    assert sha256_retry == sha256
    assert f"sha256={sha256}\n" in github_output_retry.read_text(encoding="utf-8")


def validate_pendencias_structure(content: str) -> bool:
    header = "# Pendências para próximas releases\n\n"
    if not content.startswith(header):
        return False
    body = content[len(header):]
    stripped_body = body.strip()
    if not stripped_body:
        return False
    if stripped_body == "Nenhuma pendência no momento.":
        return True

    if not body.startswith("## "):
        return False

    sections = re.split(r"(?m)^## ", body)
    if sections[0] != "":
        return False

    section_entries = sections[1:]
    if not section_entries:
        return False

    version_pattern = re.compile(r"^\d+\.\d+\.\d+\s*[-—].+")
    for sec in section_entries:
        lines = sec.splitlines()
        if not lines:
            return False
        title_line = lines[0]
        if not version_pattern.match(title_line):
            return False
        sec_content = "\n".join(lines[1:]).strip()
        if not sec_content:
            return False

    return True


def test_release_skill_structure_and_pendencias_contract() -> None:
    skill_file = ROOT / ".agents" / "skills" / "falafacil-release" / "SKILL.md"
    claude_symlink = ROOT / ".claude" / "skills" / "falafacil-release"
    omp_symlink = ROOT / ".omp" / "skills" / "falafacil-release"
    release_doc = ROOT / "docs" / "RELEASE.md"
    pendencias_doc = ROOT / "docs" / "PENDENCIAS.md"
    agents_doc = ROOT / "AGENTS.md"
    architecture_agents_doc = ROOT / "docs" / "architecture" / "agentes.md"

    # 1. Arquivo canônico da skill
    assert skill_file.is_file()
    assert not skill_file.is_symlink()

    # 2. Symlink no .claude
    assert claude_symlink.is_symlink()
    assert os.readlink(claude_symlink) == "../../.agents/skills/falafacil-release"
    assert claude_symlink.resolve() == (ROOT / ".agents" / "skills" / "falafacil-release").resolve()
    assert (claude_symlink / "SKILL.md").is_file()

    # 3. Symlink no .omp
    assert omp_symlink.is_symlink()
    assert os.readlink(omp_symlink) == "../../.agents/skills/falafacil-release"
    assert omp_symlink.resolve() == (ROOT / ".agents" / "skills" / "falafacil-release").resolve()
    assert (omp_symlink / "SKILL.md").is_file()

    # 4. Conteúdo da skill e consulta/resolução de PENDENCIAS.md + sequência e governança
    skill_content = skill_file.read_text(encoding="utf-8")
    assert "docs/PENDENCIAS.md" in skill_content
    assert "Nenhuma pendência no momento." in skill_content

    skill_step_impl = "Implement the pending changes and corresponding tests (role `implementador`)."
    skill_step_test = "Run verification suite and smoke validation (role `testador`, requires `PASS`)."
    skill_step_clean = (
        "After `PASS`, remove the resolved item from `docs/PENDENCIAS.md` (role `implementador`). "
        "When all pendencies are resolved, ensure `docs/PENDENCIAS.md` is clean "
        "(containing only `# Pendências para próximas releases\\n\\nNenhuma pendência no momento.`)."
    )
    skill_step_rev = "Review complete diff including the cleaned `docs/PENDENCIAS.md` and test evidence (role `revisor`, requires `APROVADO`)."
    skill_step_commit = "Commit the implemented pendencies to `main` branch (role: principal / release operator) before initiating version bump."

    assert skill_step_impl in skill_content
    assert skill_step_test in skill_content
    assert skill_step_clean in skill_content
    assert skill_step_rev in skill_content
    assert skill_step_commit in skill_content

    pos_sk_impl = skill_content.index(skill_step_impl)
    pos_sk_test = skill_content.index(skill_step_test)
    pos_sk_clean = skill_content.index(skill_step_clean)
    pos_sk_rev = skill_content.index(skill_step_rev)
    pos_sk_commit = skill_content.index(skill_step_commit)
    assert pos_sk_impl < pos_sk_test < pos_sk_clean < pos_sk_rev < pos_sk_commit

    assert "authorizes only the principal/release operator to commit resolved pendencies to `main`" in skill_content
    assert "Delegated roles (`implementador`, `testador`, `revisor`) remain strictly forbidden" in skill_content

    # 5. Documentação em RELEASE.md
    release_content = release_doc.read_text(encoding="utf-8")
    assert "docs/PENDENCIAS.md" in release_content
    assert "Nenhuma pendência no momento." in release_content
    assert ".agents/skills/falafacil-release/SKILL.md" in release_content

    rel_step_impl = "O `implementador` implementa o código e os testes correspondentes (preservando o registro em `docs/PENDENCIAS.md` durante a etapa)."
    rel_step_test = "O `testador` executa os testes e validação de smoke (`PASS`)."
    rel_step_clean = (
        "Após a validação com `PASS`, o `implementador` remove o item resolvido de `docs/PENDENCIAS.md` "
        "(deixando o documento limpo contendo apenas `# Pendências para próximas releases\\n\\nNenhuma pendência no momento.` se não restarem pendências)."
    )
    rel_step_rev = "O `revisor` audita o diff completo (incluindo código, testes e a limpeza de `docs/PENDENCIAS.md`) e concede `APROVADO`."
    rel_step_commit = "O agente principal / operador de release realiza o commit das pendências resolvidas na branch `main` antes de iniciar o resumo e bump de versão."

    assert rel_step_impl in release_content
    assert rel_step_test in release_content
    assert rel_step_clean in release_content
    assert rel_step_rev in release_content
    assert rel_step_commit in release_content

    pos_rel_impl = release_content.index(rel_step_impl)
    pos_rel_test = release_content.index(rel_step_test)
    pos_rel_clean = release_content.index(rel_step_clean)
    pos_rel_rev = release_content.index(rel_step_rev)
    pos_rel_commit = release_content.index(rel_step_commit)
    assert pos_rel_impl < pos_rel_test < pos_rel_clean < pos_rel_rev < pos_rel_commit

    assert "Exceção estrita de release para o agente principal / operador" in release_content
    assert "commit das pendências resolvidas na branch `main`" in release_content
    assert "commits de bump na branch `main`, push para `origin main` e criação/push da tag anotada `vX.Y.Z`" in release_content
    assert "Os papéis delegados (`implementador`, `testador`, `revisor`) continuam estritamente proibidos" in release_content

    # 6. Contrato de governança em AGENTS.md
    assert agents_doc.is_file()
    agents_content = agents_doc.read_text(encoding="utf-8")
    assert "Exceção estrita de release:" in agents_content
    assert "antes do bump de versão deve ser realizada a consulta obrigatória a `docs/PENDENCIAS.md`" in agents_content
    assert "elas são implementadas pelo `implementador` com testes" in agents_content
    assert "validadas pelo `testador` (`PASS`)" in agents_content
    assert "o item resolvido é removido de `docs/PENDENCIAS.md` pelo `implementador` (deixando o documento limpo quando não restarem pendências)" in agents_content
    assert "o diff é auditado pelo `revisor` (`APROVADO`)" in agents_content
    assert "agente principal / operador de release fica estritamente autorizado a realizar o commit das pendências resolvidas na branch `main`" in agents_content

    pos_ag_impl = agents_content.index("elas são implementadas pelo `implementador` com testes")
    pos_ag_test = agents_content.index("validadas pelo `testador` (`PASS`)")
    pos_ag_clean = agents_content.index("o item resolvido é removido de `docs/PENDENCIAS.md` pelo `implementador` (deixando o documento limpo quando não restarem pendências)")
    pos_ag_rev = agents_content.index("o diff é auditado pelo `revisor` (`APROVADO`)")
    pos_ag_commit = agents_content.index("agente principal / operador de release fica estritamente autorizado a realizar o commit das pendências resolvidas na branch `main`")
    assert pos_ag_impl < pos_ag_test < pos_ag_clean < pos_ag_rev < pos_ag_commit

    assert "commits de bump na branch `main`, push para `origin main` e criação/push da tag anotada `vX.Y.Z`" in agents_content
    assert "Nenhum papel delegado (`implementador`, `testador`, `revisor`) pode realizar commits, pushes, tags ou mutações de branch/PR" in agents_content

    # 7. Contrato de governança em docs/architecture/agentes.md
    assert architecture_agents_doc.is_file()
    arch_content = architecture_agents_doc.read_text(encoding="utf-8")
    assert "realiza a consulta prévia obrigatória a `docs/PENDENCIAS.md`" in arch_content

    arch_p1_impl = "encaminha a implementação ao `implementador` e a validação ao `testador`"
    arch_p1_pass = "após o `PASS`"
    arch_p1_clean = "o `implementador` remove o item concluído de `docs/PENDENCIAS.md` (deixando o documento limpo)"
    arch_p1_rev = "o `revisor` audita o diff com `APROVADO`"
    arch_p1_commit = "o principal fica estritamente autorizado a realizar o commit das pendências resolvidas na branch `main`"

    assert arch_p1_impl in arch_content
    assert arch_p1_pass in arch_content
    assert arch_p1_clean in arch_content
    assert arch_p1_rev in arch_content
    assert arch_p1_commit in arch_content

    pos_arch_impl = arch_content.index(arch_p1_impl)
    pos_arch_pass = arch_content.index(arch_p1_pass)
    pos_arch_clean = arch_content.index(arch_p1_clean)
    pos_arch_rev = arch_content.index(arch_p1_rev)
    pos_arch_commit = arch_content.index(arch_p1_commit)
    assert pos_arch_impl < pos_arch_pass < pos_arch_clean < pos_arch_rev < pos_arch_commit

    arch_c2_impl = "elas são implementadas pelo `implementador` com testes"
    arch_c2_test = "validadas pelo `testador` (`PASS`)"
    arch_c2_clean = "o item resolvido é removido de `docs/PENDENCIAS.md` pelo `implementador` (deixando o documento limpo quando não restarem pendências)"
    arch_c2_rev = "o diff é auditado pelo `revisor` (`APROVADO`)"
    arch_c2_commit = "o agente principal / operador de release fica estritamente autorizado a realizar o commit das pendências resolvidas na branch `main`"

    assert arch_c2_impl in arch_content
    assert arch_c2_test in arch_content
    assert arch_c2_clean in arch_content
    assert arch_c2_rev in arch_content
    assert arch_c2_commit in arch_content

    pos_c2_impl = arch_content.index(arch_c2_impl)
    pos_c2_test = arch_content.index(arch_c2_test)
    pos_c2_clean = arch_content.index(arch_c2_clean)
    pos_c2_rev = arch_content.index(arch_c2_rev)
    pos_c2_commit = arch_content.index(arch_c2_commit)
    assert pos_c2_impl < pos_c2_test < pos_c2_clean < pos_c2_rev < pos_c2_commit

    assert "Nenhum papel delegado (`implementador`, `testador`, `revisor`) pode realizar commits, pushes, tags ou mutações de branch/PR" in arch_content
    # 8. Validação estrutural de PENDENCIAS.md (casos negativos e positivos)
    # Casos negativos:
    assert not validate_pendencias_structure("")
    assert not validate_pendencias_structure("# Pendências para próximas releases")
    assert not validate_pendencias_structure("# Pendências para próximas releases\n")
    assert not validate_pendencias_structure("# Pendências para próximas releases\n\n")
    assert not validate_pendencias_structure("\n# Pendências para próximas releases\n\nNenhuma pendência no momento.")
    assert not validate_pendencias_structure("## 0.2.2 — sem header principal\n\ncorpo")
    assert not validate_pendencias_structure("# Pendências para próximas releases\n\nTexto solto antes de seção\n## 0.2.2 — teste\n\nconteúdo")
    assert not validate_pendencias_structure("# Pendências para próximas releases\n\n## 0.2.2 — teste sem corpo\n")
    assert not validate_pendencias_structure("# Pendências para próximas releases\n\n## VersaoInvalida — teste\n\nconteúdo")

    # Casos positivos:
    assert validate_pendencias_structure("# Pendências para próximas releases\n\nNenhuma pendência no momento.")
    assert validate_pendencias_structure("# Pendências para próximas releases\n\nNenhuma pendência no momento.\n")
    assert validate_pendencias_structure("# Pendências para próximas releases\n\n## 0.2.2 — teste pendencia\n\ncorpo da pendencia")

    # Documento real no repositório:
    assert pendencias_doc.is_file()
    pendencias_content = pendencias_doc.read_text(encoding="utf-8")
    assert validate_pendencias_structure(pendencias_content)
