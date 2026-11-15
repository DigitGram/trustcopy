# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DigitGram
"""TrustCopy GTK 4 / libadwaita front end: the application and its command line. One window per job (jobs run one at a time), one settings window."""
import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

import core
from cli import parser  # the command line is shared with the terminal mode
from i18n import _
from ui_common import APP_ID, CSS, show_folder, to_paths
from ui_panel import Panel
from ui_settings import Settings


class App(Adw.Application):
    """Opens one Panel per job and runs them one after the other; also owns the settings window."""

    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.running, self.waiting = None, []  # one job at a time: parallel jobs on one disk only slow each other down
        self.settings = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        for name, handler in (("preferences", self.show_settings), ("about", self.about)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)
        self.set_accels_for_action("app.preferences", ["<primary>comma"])
        open_folder = Gio.SimpleAction.new("open-folder", GLib.VariantType.new("s"))  # the button of the "job done" notification
        open_folder.connect("activate", lambda action, uri: show_folder(Gio.File.new_for_uri(uri.get_string()), self.get_active_window()))
        self.add_action(open_folder)

    def show_settings(self, *a):
        if self.settings is None:
            self.settings = Settings(self)
            self.settings.connect("close-request", self.settings_closed)
        self.settings.present()

    def settings_closed(self, *a):
        self.settings = None  # a closed window is gone; the next request builds a new one
        return False

    def about(self, *a):
        dialog = Adw.AboutDialog(
            application_name="TrustCopy", application_icon=APP_ID, version=core.__version__, developer_name=core.AUTHOR,
            developers=[core.AUTHOR], copyright=core.COPYRIGHT, license_type=Gtk.License.GPL_3_0,
            comments=_("Copy and move with a checksum check, a live speed graph and clear answers when a file already exists or fails. "
                       "Pause, cancel or retry only the files that went wrong."),
        )
        dialog.present(self.get_active_window())
        import ui_about  # imported late: nothing of it runs unless the About dialog is opened
        import ui_about_art
        ui_about.attach(dialog, ui_about_art.SCENES)

    def do_command_line(self, cmd):
        try:
            args = parser.parse_intermixed_args(cmd.get_arguments()[1:])  # options may sit between the files
        except SystemExit as e:
            return e.code if isinstance(e.code, int) else 2
        if args.op in (None, "settings"):  # started from the app grid: just the settings
            self.show_settings()
            return 0
        cwd = cmd.get_cwd() or os.getcwd()
        dest = next(iter(to_paths([args.to], cwd)), None) if args.to else None
        panel = Panel(self)
        panel.present()
        if args.to and dest is None:  # trash, recent, a place without a local path: never fall back to the current folder
            panel.fail(_("The destination is not a folder on this computer."))
        elif args.op == "paste":
            panel.paste(dest or cwd)
        elif (paths := panel.local_paths(args.files, cwd)) is not None:
            panel.begin(args.op, paths, dest)
        return 0

    def enqueue(self, panel):
        if self.running:
            self.waiting.append(panel)
            panel.say(_("Waiting for the previous job…"))
        else:
            self.running = panel
            panel.run()

    def next_job(self):
        self.running = self.waiting.pop(0) if self.waiting else None
        if self.running:
            self.running.run()

    def forget(self, panel):
        if panel in self.waiting:
            self.waiting.remove(panel)
