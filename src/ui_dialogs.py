# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DigitGram
"""Questions the job window asks: a file that already exists, and yes/no warnings."""
import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, Pango

import core
import desktop
from i18n import _
from ui_common import AFTER, describe, differences, key_value_grid


def conflict_dialog(src, dst, respond):
    """Asks what to do with a target that exists. Every way out calls respond(policy, apply to all); it can be more than once."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
    box.set_size_request(480, -1)
    heading = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)  # the triangle of the other warnings: replacing a file cannot be undone
    heading.append(Gtk.Image(icon_name="dialog-warning-symbolic", pixel_size=28, css_classes=["warning"]))
    heading.append(Gtk.Label(label=_("File already exists"), css_classes=["title-3"]))
    box.append(heading)
    box.append(Gtk.Label(label=desktop.shown(os.path.basename(dst)), ellipsize=Pango.EllipsizeMode.MIDDLE, css_classes=["heading"]))
    info, (new, old) = key_value_grid(_("New"), _("Existing"))
    info.set_halign(Gtk.Align.CENTER)
    new.set_label(describe(src))
    old.set_label(describe(dst))
    for i, (text, css) in enumerate(differences(src, dst)):  # what sets the new file apart, beside its line
        info.attach(Gtk.Label(label=text, css_classes=["caption-heading", css]), 2 + i, 0, 1, 1)
    box.append(info)
    remember = Gtk.CheckButton(label=_("Apply to all remaining conflicts"), hexpand=True)
    dialog = Adw.Dialog(child=box)

    def pick(widget, key):
        respond(key, remember.get_active())
        dialog.close()

    choices = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)  # each answer says what it does
    for key, title, subtitle, icon, css in (
            ("keep_both", _("Keep both"), _("Saves the new file as “{name}”").format(name=desktop.shown(os.path.basename(core.free_name(dst)))), "edit-copy-symbolic", "accent"),
            ("skip", _("Skip"), _("Leaves the existing file as it is"), "media-skip-forward-symbolic", None),
            ("newer", _("Overwrite if newer"), _("Replaces it only when the new file is more recent"), "document-open-recent-symbolic", None),
            ("overwrite", _("Overwrite"), _("Replaces the existing file"), "edit-delete-symbolic", "error")):
        row = Adw.ActionRow(title=title, subtitle=subtitle, activatable=True)
        row.add_prefix(Gtk.Image(icon_name=icon, css_classes=[css] if css else []))
        row.add_suffix(Gtk.Image(icon_name="go-next-symbolic", css_classes=["dim-label"]))
        row.connect("activated", pick, key)
        choices.append(row)
        if key == "keep_both":  # the focus decides what Enter does, and would start on the checkbox
            dialog.set_focus(row)
    box.append(choices)
    footer = Gtk.Box(spacing=8)
    footer.append(remember)
    cancel = Gtk.Button(label=_("Cancel"))
    cancel.connect("clicked", pick, "cancel")
    footer.append(cancel)
    box.append(footer)
    dialog.connect("closed", lambda d: respond("cancel", False))  # Escape is the Cancel button, not a skip
    return dialog


def warning_dialog(window, title, text, no, yes, on_answer, css="warning", style=Adw.ResponseAppearance.DESTRUCTIVE):
    """Question with a big triangle ("error" = red, "warning" = yellow) beside the text.
    `on_answer(True)` runs for `yes`; `no`, Escape and closing the dialog give `on_answer(False)`."""
    dialog = Adw.AlertDialog.new(title, None)
    box = Gtk.Box(spacing=16, halign=Gtk.Align.CENTER, margin_top=6)
    box.append(Gtk.Image(icon_name="dialog-warning-symbolic", pixel_size=48, css_classes=[css]))
    box.append(Gtk.Label(label=text, xalign=0, wrap=True, hexpand=True))
    # the dialog is as wide as its content (set_content_width crashes here), never wider than the window; a window that is
    # not drawn yet (the settings reopened in a new language) has no width, only its default
    box.set_size_request(min(480, (window.get_width() or window.get_default_size()[0]) - 64), -1)
    dialog.set_extra_child(box)
    dialog.add_response("no", no)
    dialog.add_response("yes", yes)
    dialog.set_response_appearance("yes", style)
    dialog.set_close_response("no")
    dialog.choose(window, None, lambda d, res: on_answer(d.choose_finish(res) == "yes"))


def confirm_always(selector, window):
    """Asks before "Always close" sticks; No puts the previous choice back."""
    always = AFTER.index("always")
    last = [selector.get_selected()]

    def answered(yes):
        if yes:
            last[0] = always
        else:
            selector.set_selected(last[0])

    def changed(*a):
        now = selector.get_selected()
        if now != always or last[0] == always:
            last[0] = now
            return
        warning_dialog(window, _("Always close the window?"),
                       _("The window closes even if files failed – you will no longer see any errors.") + "\n\n" + _("Are you really sure?"),
                       _("No"), _("Yes"), answered, css="error")

    selector.connect("notify::selected", changed)  # connect after the initial set_selected
