#!/bin/sh
# Run the ramdisk tool from this checkout: ./ramdisk.sh [command] [--dry-run] [--force]
# Works through a symlink too, e.g. ln -s "$PWD/ramdisk.sh" ~/.local/bin/ramdisk
here=$(dirname -- "$(readlink -f -- "$0")")
PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m ramdisk "$@"
