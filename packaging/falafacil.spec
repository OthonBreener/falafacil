from pathlib import Path


PROJECT_ROOT = Path(SPECPATH).resolve().parent
SRC_ROOT = PROJECT_ROOT / "src"
PORTAUDIO_PATH = Path("/usr/lib/x86_64-linux-gnu/libportaudio.so.2")

if not PORTAUDIO_PATH.is_file():
    raise FileNotFoundError(
        f"Biblioteca PortAudio obrigatória não encontrada: {PORTAUDIO_PATH}"
    )

analysis = Analysis(
    [str(SRC_ROOT / "falafacil" / "__main__.py")],
    pathex=[str(SRC_ROOT)],
    binaries=[(str(PORTAUDIO_PATH), ".")],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="falafacil",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)
