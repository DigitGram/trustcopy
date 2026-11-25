#!/usr/bin/python3
# not "env python3": the GTK bindings are distro packages for the system's Python; a conda, pyenv or venv python3 first in PATH has none
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DigitGram
"""TrustCopy: verified copying and moving for Files (Nautilus), with speed graph and conflict handling.

Entry point. The menu entries start it as
  trustcopy copy|move [--to FOLDER] [--] FILE...      trustcopy paste [--to FOLDER]
and the installer runs the GTK-free `integrate` command:
  trustcopy integrate [--remove]
and, as text too, `trustcopy clean FOLDER... [--yes]` deletes the leftovers of a crashed copy.
From a terminal, `copy` and `move` with --to answer there, as text (cli.py); --gui asks for the window.
Without arguments the settings open. ui_gtk.py shows the windows, core.py (copying) and desktop.py (settings, right-click entries) do the work.
"""
import sys

import core
import desktop
import i18n
from i18n import _


def main():
    i18n.set_language(desktop.load_config()["language"])
    desktop.sync_entries()  # python3-nautilus gone since the last start: back to the Scripts entries
    if sys.argv[1:2] == ["--version"]:
        print(f"TrustCopy {core.__version__}")
        return 0
    if sys.argv[1:2] == ["integrate"]:  # headless: the installer needs no display
        msg = desktop.install_scripts(remove_only="--remove" in sys.argv[2:])
        if msg:
            print(msg, file=sys.stderr)
        return 1 if msg and not isinstance(msg, desktop.Note) else 0
    if sys.argv[1:2] == ["clean"]:  # text only, like `integrate`: no window for it
        import cli
        return cli.clean(sys.argv[2:])
    if sys.stdout.isatty() and sys.argv[1:2] in (["copy"], ["move"]):  # a menu entry has no terminal: it gets the window
        import cli
        args = cli.parser.parse_intermixed_args(sys.argv[1:])
        if args.to and not args.gui:  # without a destination the window asks for the folder, as it always did
            return cli.run(args)
    try:
        import ui_gtk  # imported late: the commands above work without GTK
    except (ImportError, ValueError):  # python3-gi, GTK 4 or libadwaita >= 1.5 missing
        return _("TrustCopy needs GTK 4 with libadwaita 1.5 or newer (python3-gi, gir1.2-adw-1).")
    return ui_gtk.App().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
