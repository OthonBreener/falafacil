#!/bin/sh
set -eu
umask 0077

if [ "$#" -ne 1 ]; then
    printf 'Usage: %s EXECUTABLE\n' "$0" >&2
    exit 2
fi

SOURCE=$1
if [ ! -f "$SOURCE" ] || [ ! -x "$SOURCE" ]; then
    printf 'Executable is missing or not executable: %s\n' "$SOURCE" >&2
    exit 1
fi

NEWLINE='
'
TAB=$(printf '\t')
CR=$(printf '\r')

reject_desktop_path() {
    path=$1
    case $path in
        *[![:print:]]*)
            printf 'Installed path contains non-printable or non-ASCII character; refusing desktop entry\n' >&2
            exit 1
            ;;
        *%*)
            printf 'Installed path contains %%; refusing desktop entry\n' >&2
            exit 1
            ;;
        *=*)
            printf 'Installed path contains =; refusing desktop entry\n' >&2
            exit 1
            ;;
    esac
}

case ${HOME:?HOME must be set} in
    *[![:print:]]*)
        printf 'HOME contains non-printable or non-ASCII character; refusing desktop entry\n' >&2
        exit 1
        ;;
    *%*)
        printf 'HOME contains %%; refusing desktop entry\n' >&2
        exit 1
        ;;
    *=*)
        printf 'HOME contains =; refusing desktop entry\n' >&2
        exit 1
        ;;
esac

HOME_DIR=$(CDPATH= cd -- "${HOME:?HOME must be set}" && pwd)
SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$SOURCE")" && pwd)
SOURCE="$SOURCE_DIR/$(basename -- "$SOURCE")"
INSTALL_DIR="$HOME_DIR/.local/bin"
DEST="$INSTALL_DIR/falafacil"
DESKTOP_ENTRY="$HOME_DIR/.local/share/applications/falafacil.desktop"

reject_symlink_path() {
    path=$1
    while :; do
        if [ -L "$path" ]; then
            printf 'Refusing symlinked install path component: %s\n' "$path" >&2
            exit 1
        fi
        [ "$path" = "/" ] && break
        path=$(dirname -- "$path")
    done
}

reject_desktop_path "$DEST"
reject_symlink_path "$INSTALL_DIR"

LOCAL_DIR="$HOME_DIR/.local"
LOCAL_DIR_EXISTED=0
INSTALL_DIR_EXISTED=0
if [ -e "$LOCAL_DIR" ] || [ -L "$LOCAL_DIR" ]; then
    LOCAL_DIR_EXISTED=1
fi
if [ -e "$INSTALL_DIR" ] || [ -L "$INSTALL_DIR" ]; then
    INSTALL_DIR_EXISTED=1
fi

if [ "$LOCAL_DIR_EXISTED" -eq 0 ]; then
    mkdir -p "$LOCAL_DIR"
    chmod 0755 "$LOCAL_DIR"
else
    chmod go-w "$LOCAL_DIR"
fi

if [ "$INSTALL_DIR_EXISTED" -eq 0 ]; then
    mkdir -p "$INSTALL_DIR"
    chmod 0755 "$INSTALL_DIR"
else
    chmod go-w "$INSTALL_DIR"
fi

reject_symlink_path "$INSTALL_DIR"

reject_unsafe_destination() {
    destination=$1
    if [ -L "$destination" ]; then
        printf 'Refusing symlink destination: %s\n' "$destination" >&2
        exit 1
    fi
    if [ -d "$destination" ]; then
        printf 'Refusing directory destination: %s\n' "$destination" >&2
        exit 1
    fi
    if [ -e "$destination" ] && [ ! -f "$destination" ]; then
        printf 'Refusing non-regular destination: %s\n' "$destination" >&2
        exit 1
    fi
}

TEMP_EXEC=
cleanup() {
    if [ -n "$TEMP_EXEC" ]; then
        rm -f -- "$TEMP_EXEC"
    fi
}
trap cleanup EXIT HUP INT TERM

reject_unsafe_destination "$DEST"
TEMP_EXEC=$(mktemp "$INSTALL_DIR/.falafacil.XXXXXX")
cp -- "$SOURCE" "$TEMP_EXEC"
chmod 0755 "$TEMP_EXEC"
reject_unsafe_destination "$DEST"
mv -f -- "$TEMP_EXEC" "$DEST"
TEMP_EXEC=

"$DEST" --install-user-desktop "$DEST"

printf 'Installed executable: %s\n' "$DEST"
printf 'Installed desktop entry: %s\n' "$DESKTOP_ENTRY"
