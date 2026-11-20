# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DigitGram
"""The settings window."""
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, GObject, Gtk

import core
import desktop
import i18n
from i18n import _
from ui_common import AFTER, ALGOS, CONFLICTS, after_labels
from ui_dialogs import confirm_always, speed_dialog, warning_dialog


class Settings(Adw.ApplicationWindow):
    """Every change is saved at once (no OK button), like GNOME settings."""

    def __init__(self, app):
        super().__init__(application=app, title=_("Settings"), default_width=700, default_height=760)
        self.cfg = desktop.load_config()
        stack = Adw.ViewStack()
        for title, icon, groups in ((_("General"), "preferences-system-symbolic", (self._general_group(), self._context_group(), *self._update_groups(), self._info_group())),
                                    (_("Copying"), "edit-copy-symbolic", (self._copying_group(), self._finished_group()))):
            page = Adw.PreferencesPage()
            for group in groups:
                page.add(group)
            stack.add_titled_with_icon(page, title, title, icon)
        self.toasts = Adw.ToastOverlay(child=stack)
        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(Adw.HeaderBar(title_widget=Adw.ViewSwitcher(stack=stack, policy=Adw.ViewSwitcherPolicy.WIDE)))
        self.set_content(view)

    def _general_group(self):
        languages = i18n.languages()
        language = self.combo(_("Language"), "", [_("System default"), *languages.values()], ["auto", *languages], "language")
        general = Adw.PreferencesGroup()  # the page is called "General"
        general.add(language)
        return general

    def _copying_group(self):
        verify = self.switch(_("Verify files after copying"), _("Reads every copy back from the disk and compares checksums."), "verify")
        algo = self.combo(_("Checksum"), _("CRC32 is fastest and catches accidental corruption; SHA-256 is cryptographically secure."), [_("CRC32 (fast)"), _("SHA-256 (thorough)")], ALGOS, "algo")
        verify.bind_property("active", algo, "sensitive", GObject.BindingFlags.SYNC_CREATE)
        conflict = self.combo(_("If a file exists"), _("What happens when a file with that name is already in the target."),
                              [_("Ask every time"), _("Skip"), _("Keep both"), _("Overwrite if newer"), _("Overwrite")], CONFLICTS, "conflict")
        # Long explanations go into the group descriptions: next to a drop-down they squeeze its text ("Close on succ…")
        copying = Adw.PreferencesGroup(title=_("Copying"))
        for row in (verify, algo, conflict):
            copying.add(row)
        sums = self.switch(_("Write a checksum file"), _("Saves a list of checksums in the target folder. Slower, because every file has to be read."), "sums")
        kind = self.combo(_("Algorithm"), _("SHA-256 is the safe standard, CRC32 the fastest."), ["SHA-512", "SHA-256", "SHA-1", "MD5", _("CRC32 (SFV)")], tuple(core.SUM_ALGOS), "sums_algo")
        sums.bind_property("active", kind, "sensitive", GObject.BindingFlags.SYNC_CREATE)
        for row in (sums, kind):
            copying.add(row)
        copying.add(self._speed_row())
        return copying

    def _speed_row(self):
        """A button that measures every checksum on this computer and shows the result in a window of its own."""
        self.speed_button = Gtk.Button(label=_("Test"), valign=Gtk.Align.CENTER)
        self.speed_button.connect("clicked", self.test_speeds)
        row = Adw.ActionRow(title=_("Algorithm speed"), subtitle=_("Measures how fast this computer calculates each algorithm and recommends one."))
        row.add_suffix(self.speed_button)
        row.set_activatable_widget(self.speed_button)
        return row

    def test_speeds(self, button):
        button.set_sensitive(False)
        button.set_label(_("Testing…"))
        threading.Thread(target=lambda: GLib.idle_add(self.show_speeds, core.hash_speeds()), daemon=True).start()

    def show_speeds(self, speeds):
        self.speed_button.set_label(_("Test"))
        self.speed_button.set_sensitive(True)
        self.speed_dialog = speed_dialog(speeds)
        self.speed_dialog.present(self)

    def _finished_group(self):
        close = self.combo(_("Window"), "", after_labels(), AFTER, "after")
        confirm_always(close, self)
        notify = self.switch(_("Notifications"), _("Shows a message when a job ends while the window is in the background."), "notify")
        sound = self.switch(_("Sound"), _("Plays a sound when a job ends."), "sound")
        finished = Adw.PreferencesGroup(title=_("When finished"))  # no word about "Always close": choosing it asks first
        for row in (close, notify, sound):
            finished.add(row)
        return finished

    def _context_group(self):
        menu = self.combo(_("Entries"), "", [_("Under Scripts"), _("In a submenu")], ("scripts", "folder"), "menu")
        context = Adw.PreferencesGroup(title=_("Context menu"), description=_("In the submenu “TrustCopy” they take one more click."))
        context.add(menu)
        context.add(self._shortcuts_row())
        return context

    def _shortcuts_row(self):
        """The switch with the three actions and their keys below it (they unfold when it is on)."""
        row = Adw.ExpanderRow(title=_("Keyboard shortcuts"), subtitle=_("Only possible in Files (Nautilus)"), show_enable_switch=True, enable_expansion=self.cfg["shortcuts"], expanded=self.cfg["shortcuts"])
        row.connect("notify::enable-expansion", lambda r, p: self.apply("shortcuts", r.get_enable_expansion()))
        self.key_labels = {}
        for (op, title), key in zip(desktop.SCRIPT_ACTIONS.items(), desktop.shortcut_keys().values()):
            self.key_labels[op] = Gtk.ShortcutLabel(accelerator=key, valign=Gtk.Align.CENTER)
            change = Gtk.Button(icon_name="document-edit-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"], tooltip_text=_("Change shortcut"))
            change.connect("clicked", self.ask_key, op, title)
            line = Adw.ActionRow(title=_(title))
            line.add_suffix(self.key_labels[op])
            line.add_suffix(change)
            row.add_row(line)
        return row

    def ask_key(self, button, op, title):
        """Waits for the new key of `op`: Escape cancels, Backspace brings the default back, a key that cannot be used is explained."""
        dialog = Adw.AlertDialog.new(_(title), _("Press the new shortcut. Esc cancels, Backspace restores the default."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.set_close_response("cancel")
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)

        def pressed(controller, keyval, keycode, state):
            if keyval == Gdk.KEY_Escape:
                return False  # the dialog closes itself
            if keyval == Gdk.KEY_BackSpace:
                accel = desktop.ACCEL_DEFAULTS[op]
            else:
                mods = state & Gtk.accelerator_get_default_mod_mask()
                if not Gtk.accelerator_valid(keyval, mods):  # Ctrl alone, say
                    return True
                accel = Gtk.accelerator_name(Gdk.keyval_to_lower(keyval), mods)
            problem = desktop.key_problem(accel, op)
            if problem:
                dialog.set_body(f"{desktop.accel_label(accel)}: {problem}")
                return True
            dialog.force_close()
            self.set_key(op, accel)
            return True

        keys.connect("key-pressed", pressed)
        dialog.add_controller(keys)
        dialog.present(self)

    def set_key(self, op, accel):
        """Keeps the chosen key (only those that differ from the default are saved)."""
        chosen = {**desktop.shortcut_keys(), op: accel}
        self.key_labels[op].set_accelerator(accel)
        self.apply("keys", {o: a for o, a in chosen.items() if a != desktop.ACCEL_DEFAULTS[o]})

    def _update_groups(self):
        """A switch for the weekly check at start (off by default) and a row that checks right now and then says what it found."""
        if not desktop.update_check_offered():
            return ()
        group = Adw.PreferencesGroup(title=_("Updates"))
        group.add(self.switch(_("Check for updates weekly"), _("Asks GitHub once a week for the newest version. Nothing is sent and nothing is installed."), "update_check"))
        self.update_page = None  # set once a newer release is found: the button then opens its page
        self.update_row = Adw.ActionRow(title=_("Installed version: {version}").format(version=core.__version__))
        self.update_button = Gtk.Button(label=_("Check now"), valign=Gtk.Align.CENTER)
        self.update_button.connect("clicked", self.check_update)
        self.update_row.add_suffix(self.update_button)
        self.update_row.set_activatable_widget(self.update_button)
        group.add(self.update_row)
        return (group,)

    def check_update(self, button):
        if self.update_page:
            Gtk.UriLauncher(uri=self.update_page).launch(self, None, None)
            return
        button.set_sensitive(False)

        def work():
            try:
                found, error = desktop.newer_release(), None
            except (OSError, ValueError, KeyError) as e:
                found, error = None, e
            GLib.idle_add(self.show_update, found, error)
        threading.Thread(target=work, daemon=True).start()

    def show_update(self, found, error):
        self.update_button.set_sensitive(True)
        if error:
            self.toast(_("Could not check for updates. Are you online?"))
            return
        if found:  # the row itself says it, and its button leads to the release notes and the way to update
            self.update_page = found[1]
            self.update_row.set_title(_("Update available: {version}").format(version=found[0]))
            self.update_button.set_label(_("Release notes"))
        else:
            self.update_row.set_title(_("TrustCopy is up to date."))
        self.update_row.set_subtitle(_("Installed version: {version}").format(version=core.__version__))

    def _info_group(self):
        reset = Gtk.Button(label=_("Reset"), valign=Gtk.Align.CENTER, css_classes=["destructive-action"])
        reset.connect("clicked", self.confirm_reset)
        reset_row = Adw.ActionRow(title=_("Reset settings"), subtitle=_("Back to the default values."))
        reset_row.add_suffix(reset)
        about = Adw.ActionRow(title=_("About TrustCopy"), activatable=True)
        about.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        about.connect("activated", lambda *a: self.get_application().about())
        info = Adw.PreferencesGroup(title=_("Other"))
        info.add(reset_row)
        info.add(about)
        return info

    def combo(self, title, subtitle, labels, values, key):
        row = Adw.ComboRow(title=title, subtitle=subtitle, model=Gtk.StringList.new(labels))
        row.set_selected(values.index(self.cfg[key]))
        row.connect("notify::selected", lambda r, p: self.apply(key, values[r.get_selected()]))
        return row

    def switch(self, title, subtitle, key):
        row = Adw.SwitchRow(title=title, subtitle=subtitle, active=self.cfg[key])
        row.connect("notify::active", lambda r, p: self.apply(key, r.get_active()))
        return row

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, use_markup=False))  # an error text may hold a path with "&"

    def apply(self, key, value):
        """Saves one setting at once; a new language or menu style renames or moves the right-click entries."""
        before = desktop.files_setup()
        error = desktop.set_option(key, value)
        # idle: a language change replaces this window, which must not happen inside its own drop-down's signal
        GLib.idle_add(self.finish_change, before, error, key == "language")

    def finish_change(self, before, error, reopen):
        """Shows the error or the keys left out, and offers a restart to each running file manager that would read
        other entries or shortcuts."""
        window = self.reopen() if reopen else self
        if error:
            window.toast(error)
        after = desktop.files_setup()
        running = desktop.files_running([fm for fm in after if after[fm] != before[fm]])
        if running:
            window.offer_restart(running)

    def reopen(self):
        """A new window shows the new language or the defaults."""
        app = self.get_application()
        self.close()
        app.show_settings()
        return app.settings

    def confirm_reset(self, *a):
        warning_dialog(self, _("Reset all settings?"), _("Language, checksum and everything else go back to their defaults."),
                       _("Cancel"), _("Reset"), self.reset_chosen)

    def reset_chosen(self, yes):
        if yes:
            before = desktop.files_setup()
            GLib.idle_add(self.finish_change, before, desktop.reset_config(), True)  # like apply(): not inside the dialog's answer

    def offer_restart(self, programs):
        """The file managers only show changed script entries and Files uses changed shortcuts after a restart."""
        if programs == ["nautilus"]:
            # TRANSLATORS: "Files" is the name of the GNOME file manager (Nautilus), "Dateien" in German; put the name in quotes the way your language does
            text = _("The file manager (“Files”) has to restart to apply the change. Open windows close, and running file operations may be cancelled.")
        else:
            # TRANSLATORS: {names} is a list of program names, such as "Nautilus, Nemo"
            text = _("A restart of {names} applies the change. Open windows close, and running file operations may be cancelled.").format(
                names=", ".join(fm.title() for fm in programs))
        warning_dialog(self, _("Restart the file manager?"), text, _("Later"), _("Restart"),
                       lambda yes: self.restart_chosen(yes, programs), style=Adw.ResponseAppearance.SUGGESTED)

    def restart_chosen(self, yes, programs):
        if yes:
            desktop.restart_files(self.get_application(), self.toast, programs)
