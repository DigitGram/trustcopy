#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DigitGram
# Installs this checkout into a throw-away home, starts it and removes it again: nothing of TrustCopy may stay.
# Usage: tests/install_test.sh   (not as root: install.sh refuses that)
set -euo pipefail
cd "$(dirname "$0")/.."
HOME="$(mktemp -d)"; export HOME
trap 'rm -rf "$HOME"' EXIT
unset XDG_DATA_HOME XDG_CONFIG_HOME   # so both lie in the throw-away home
export GSETTINGS_BACKEND=memory       # the desktop's settings service is not in that home: never ask it
mkdir "$HOME/bin"
printf '#!/bin/sh\nexit 1\n' > "$HOME/bin/python3"; chmod +x "$HOME/bin/python3"
PATH="$HOME/bin:$PATH"                # a python3 without GTK first in PATH, as conda and pyenv put one: the install must not use it

./install.sh > /dev/null
[ "$("$HOME/.local/bin/trustcopy" --version)" = "TrustCopy $(sed -n 's/^VERSION="\([^"]*\)".*/\1/p' install.sh)" ]
find "$HOME/.local/share/nautilus/scripts" -name 'TrustCopy*' | grep -q .   # the right-click entries
"$HOME/.local/share/trustcopy/install.sh" --purge > /dev/null
if find "$HOME" -iname '*trustcopy*' | grep . || grep -rli trustcopy "$HOME"; then
  echo "left behind after --purge (listed above)" >&2; exit 1
fi
echo "install test ok"
