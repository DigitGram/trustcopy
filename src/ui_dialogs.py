# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DigitGram
"""Questions the job window asks: a file that already exists, and yes/no warnings."""
import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango

import core
import desktop
from i18n import _, _n
from ui_common import AFTER, describe, key_value_grid, leftover_rows, pretty


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
    text, kind = core.compare(src, dst)  # one hint beside the line of the new file: blue (the colour of the verifying step: the accent colour may be orange) the same, green only in its favour, orange if something is against it
    info.attach(Gtk.Label(label=text, css_classes=["caption-heading", {"same": "k-state-verify", "good": "success", "bad": "warning"}[kind]]), 2, 0, 1, 1)
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


def cleanup_dialog(found, on_answer):
    """Asks whether to delete the unfinished copies (`name.trustpart`) of a job that stopped suddenly, in the look of the conflict dialog:
    a heading, a few words of reassurance, the files as a card with their sizes. `on_answer(True)` for Delete; Cancel and Escape give False."""
    folder, rows, more = leftover_rows(found)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
    box.set_size_request(480, -1)
    heading = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
    heading.append(Gtk.Image(icon_name="dialog-question-symbolic", pixel_size=28))
    heading.append(Gtk.Label(label=_n("Delete {n} unfinished copy?", "Delete {n} unfinished copies?", len(found)).format(n=len(found)), css_classes=["title-3"]))
    box.append(heading)
    box.append(Gtk.Label(label=_("These files end in .trustpart. They are unfinished copies from a job that stopped suddenly, for example by a crash or a power cut. "
                                  "They are <b>of no use</b> any more, so deleting them is <b>safe</b> and recommended. Your original files and everything else stay untouched."),
                         use_markup=True, wrap=True, justify=Gtk.Justification.CENTER, css_classes=["dim-label"]))
    if folder:  # all in one folder: it is said once, above the card
        place = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        place.append(Gtk.Image(icon_name="folder-symbolic", css_classes=["dim-label"]))
        place.append(Gtk.Label(label=desktop.shown(pretty(folder)), ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=48, tooltip_text=desktop.shown(folder), css_classes=["heading"]))
        box.append(place)
    card = Gtk.ListBox(css_classes=["boxed-list", "k-slim"], selection_mode=Gtk.SelectionMode.NONE)
    for title, subtitle, size, path in rows:
        row = Adw.ActionRow(title=desktop.shown(title), subtitle=desktop.shown(subtitle) if subtitle else "", use_markup=False, title_lines=1, subtitle_lines=1, tooltip_text=desktop.shown(path))
        row.add_prefix(Gtk.Image(icon_name="text-x-generic-symbolic", css_classes=["dim-label"]))
        row.add_suffix(Gtk.Label(label=GLib.format_size(size), css_classes=["dim-label"]))
        card.append(row)
    if more:  # the rest is counted, and a tooltip names the next few
        count, size, names = more
        row = Adw.ActionRow(title=_("… and {n} more").format(n=count), use_markup=False, tooltip_text=desktop.shown(names))
        row.add_suffix(Gtk.Label(label=GLib.format_size(size), css_classes=["dim-label"]))
        card.append(row)
    box.append(card)
    answered = []
    dialog = Adw.Dialog(child=box)

    def answer(widget, yes):
        answered.append(yes)
        on_answer(yes)
        dialog.close()

    cancel, delete = Gtk.Button(label=_("Cancel")), Gtk.Button(label=_("Delete"), css_classes=["destructive-action"])
    cancel.connect("clicked", answer, False)
    delete.connect("clicked", answer, True)
    footer = Gtk.Box(spacing=8, halign=Gtk.Align.END, margin_top=6)
    footer.append(cancel)
    footer.append(delete)
    box.append(footer)
    dialog.set_focus(cancel)  # Enter is the safe answer
    dialog.connect("closed", lambda d: on_answer(False) if not answered else None)  # Escape
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
