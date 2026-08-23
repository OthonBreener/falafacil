#!/bin/sh
set -eu

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
        *"$NEWLINE"*)
            printf 'Installed path contains a newline; refusing desktop entry\n' >&2
            exit 1
            ;;
        *"$TAB"*)
            printf 'Installed path contains a tab; refusing desktop entry\n' >&2
            exit 1
            ;;
        *"$CR"*)
            printf 'Installed path contains a carriage return; refusing desktop entry\n' >&2
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
    *"$NEWLINE"*)
        printf 'HOME contains a newline; refusing desktop entry\n' >&2
        exit 1
        ;;
    *"$TAB"*)
        printf 'HOME contains a tab; refusing desktop entry\n' >&2
        exit 1
        ;;
    *"$CR"*)
        printf 'HOME contains a carriage return; refusing desktop entry\n' >&2
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
APPLICATIONS_DIR="$HOME_DIR/.local/share/applications"
DEST="$INSTALL_DIR/falafacil"
DESKTOP_ENTRY="$APPLICATIONS_DIR/falafacil.desktop"

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
reject_desktop_path "$DESKTOP_ENTRY"
reject_symlink_path "$INSTALL_DIR"
reject_symlink_path "$APPLICATIONS_DIR"
mkdir -p "$INSTALL_DIR" "$APPLICATIONS_DIR"
reject_symlink_path "$INSTALL_DIR"
reject_symlink_path "$APPLICATIONS_DIR"

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

desktop_escape() {
    # Exec is a quoted command-line field; preserve its existing escaping.
    printf '%s' "$1" | sed 's/\\/\\\\\\\\/g; s/"/\\"/g; s/`/\\`/g; s/[$]/\\$/g'
}

generic_escape() {
    # TryExec is a generic string: spaces stay literal, while backslashes
    # and semicolons must be escaped for desktop-entry parsing.
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/;/\\;/g'
}

TEMP_EXEC=
TEMP_DESKTOP=
cleanup() {
    if [ -n "$TEMP_EXEC" ]; then
        rm -f -- "$TEMP_EXEC"
    fi
    if [ -n "$TEMP_DESKTOP" ]; then
        rm -f -- "$TEMP_DESKTOP"
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

ESCAPED_DEST=$(desktop_escape "$DEST")
GENERIC_ESCAPED_DEST=$(generic_escape "$DEST")
reject_unsafe_destination "$DESKTOP_ENTRY"
TEMP_DESKTOP=$(mktemp "$APPLICATIONS_DIR/.falafacil.desktop.XXXXXX")
cat > "$TEMP_DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=FalaFácil
Comment=Transcrição de voz em português com Gemini
Exec="$ESCAPED_DEST"
TryExec=$GENERIC_ESCAPED_DEST
Terminal=false
Categories=Utility;AudioVideo;
EOF
chmod 0644 "$TEMP_DESKTOP"
reject_unsafe_destination "$DESKTOP_ENTRY"
mv -f -- "$TEMP_DESKTOP" "$DESKTOP_ENTRY"
TEMP_DESKTOP=

printf 'Installed executable: %s\n' "$DEST"
printf 'Installed desktop entry: %s\n' "$DESKTOP_ENTRY"
