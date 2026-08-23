#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
cd "$REPO_ROOT"

env -u GEMINI_API_KEY -u GOOGLE_API_KEY \
    poetry run pyinstaller --clean --noconfirm packaging/falafacil.spec

printf 'Built executable: %s\n' "$REPO_ROOT/dist/falafacil"
