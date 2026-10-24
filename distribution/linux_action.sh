#!/usr/bin/env bash
set -u
cd -- "$(dirname -- "${BASH_SOURCE[0]}")" || exit 1
starter="$PWD/${BASH_SOURCE[0]##*/}"

# File managers do not necessarily supply a terminal for executable scripts.
# Re-enter this exact starter in a visible terminal, preserving argument boundaries.
if [ "${1-}" = "--x4lan-terminal" ]; then
    shift
elif ! [ -t 0 ] || ! [ -t 1 ]; then
    if command -v konsole >/dev/null 2>&1; then
        exec konsole --separate -e bash "$starter" --x4lan-terminal "$@"
    elif command -v gnome-terminal >/dev/null 2>&1; then
        exec gnome-terminal --title="X4 Multiplayer" -- bash "$starter" --x4lan-terminal "$@"
    elif command -v xfce4-terminal >/dev/null 2>&1; then
        exec xfce4-terminal --title="X4 Multiplayer" -x bash "$starter" --x4lan-terminal "$@"
    elif command -v xterm >/dev/null 2>&1; then
        exec xterm -T "X4 Multiplayer" -e bash "$starter" --x4lan-terminal "$@"
    fi
    printf '%s\n' 'ERROR: No supported terminal found. Open a terminal in this folder and run:' >&2
    printf 'bash %q\n' "$starter" >&2
    exit 1
fi

# A failed terminal handoff must never recurse or attempt invisible input.
if ! [ -t 0 ] || ! [ -t 1 ]; then
    printf '%s\n' 'ERROR: No interactive terminal. Open a terminal and run this starter there.' >&2
    exit 1
fi
{{PYTHON}} -I -B launcher.py {{ACTION}} "$@"
result=$?
read -r -p "Press Enter to close... " answer
exit "$result"
