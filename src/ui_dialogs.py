# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DigitGram
"""Questions the job window asks: a file that already exists, and yes/no warnings."""
import os
import threading
import time
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango

import core
import desktop
from i18n import _, _n
from ui_common import AFTER, SPEED_NAMES, describe, key_value_grid, leftover_rows, pretty


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


def _answer_rows(box, rows, pick):
    """The answers of a question as a card of rows, each saying what it does (as in the conflict dialog). Returns the first row."""
    choices = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
    first = None
    for key, title, subtitle, icon, css in rows:
        row = Adw.ActionRow(title=title, subtitle=subtitle, activatable=True, use_markup=False)
        row.add_prefix(Gtk.Image(icon_name=icon, css_classes=[css] if css else []))
        row.add_suffix(Gtk.Image(icon_name="go-next-symbolic", css_classes=["dim-label"]))
        row.connect("activated", pick, key)
        choices.append(row)
        first = first or row
    box.append(choices)
    return first


def _question(title, text):
    """The frame of the two checksum-file questions: a heading with a question mark and a few calm words. Returns the dialog's box."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
    box.set_size_request(480, -1)
    heading = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
    heading.append(Gtk.Image(icon_name="dialog-question-symbolic", pixel_size=28))
    heading.append(Gtk.Label(label=title, css_classes=["title-3"]))
    box.append(heading)
    if text:
        box.append(Gtk.Label(label=text, wrap=True, justify=Gtk.Justification.CENTER, css_classes=["dim-label"]))
    return box


def sums_gap_dialog(missing, total, size, respond):
    """The checksum file was switched on while the job ran, or files were only renamed: `missing` of `total` files have no checksum yet.
    respond("all") reads them now (`size` bytes), respond("some") leaves them out, respond("none") writes no file; Escape is "none"."""
    text = _n("{n} of {total} file has no checksum yet: it was done before the checksum file was switched on, or only renamed.",
              "{n} of {total} files have no checksum yet: they were done before the checksum file was switched on, or only renamed.", missing).format(n=missing, total=total)
    box = _question(_("Checksums for all files?"), text)
    dialog = Adw.Dialog(child=box)
    answered = []

    def pick(widget, key):
        answered.append(key)
        respond(key)
        dialog.close()

    rows = [("all", _n("Include {n} file", "Include all {n} files", total).format(n=total), _("Reads the rest now: {size}").format(size=GLib.format_size(size)), "document-save-symbolic", "accent")]
    if missing < total:  # when none has one, leaving them out would leave nothing
        rows.append(("some", _("Only those that have one"), _("The checksum file is incomplete"), "list-remove-symbolic", None))
    rows.append(("none", _("No checksum file"), _("Nothing is written"), "window-close-symbolic", None))
    dialog.set_focus(_answer_rows(box, rows, pick))
    dialog.connect("closed", lambda d: respond("none") if not answered else None)
    return dialog


def sums_conflict_dialog(path, entries, mtime, new_entries, respond):
    """A checksum file with this name is there already. respond("update"|"keep_both"|"overwrite"|"skip"); Escape is "skip"."""
    box = _question(_("Checksum file already exists"), "")
    box.append(Gtk.Label(label=desktop.shown(os.path.basename(path)), ellipsize=Pango.EllipsizeMode.MIDDLE, css_classes=["heading"]))
    info, (new, old) = key_value_grid(_("New"), _("Existing"))
    info.set_halign(Gtk.Align.CENTER)
    new.set_label(_n("{n} entry", "{n} entries", new_entries).format(n=new_entries))
    old.set_label(_n("{n} entry", "{n} entries", len(entries)).format(n=len(entries)) + f" · {datetime.fromtimestamp(mtime):%x %X}")
    box.append(info)
    dialog = Adw.Dialog(child=box)
    answered = []

    def pick(widget, key):
        answered.append(key)
        respond(key)
        dialog.close()

    rows = [("update", _("Update"), _("Replaces the entries of these files and keeps the others"), "view-refresh-symbolic", "accent"),
            ("keep_both", _("Keep both"), _("Saves the new file as “{name}”").format(name=desktop.shown(os.path.basename(core.free_name(path)))), "edit-copy-symbolic", None),
            ("overwrite", _("Overwrite"), _("Replaces the existing file"), "edit-delete-symbolic", "error"),
            ("skip", _("Skip"), _("Leaves the existing file as it is"), "media-skip-forward-symbolic", None)]
    dialog.set_focus(_answer_rows(box, rows, pick))
    dialog.connect("closed", lambda d: respond("skip") if not answered else None)
    return dialog


POWER_COUNTDOWN = 60


def power_dialog(action, on_go, seconds=POWER_COUNTDOWN):
    """The countdown before the computer is suspended or shut down after a job ("suspend" or "poweroff"). `on_go()` runs once, when the seconds are
    over or on "now"; Cancel and Escape stop it."""
    suspend = action == "suspend"
    dialog = Adw.AlertDialog.new(_("Suspending") if suspend else _("Shutting down"), None)
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("now", _("Suspend now") if suspend else _("Shut down now"))
    dialog.set_default_response("cancel")  # Enter is the safe answer
    dialog.set_close_response("cancel")
    dialog.set_response_appearance("now", Adw.ResponseAppearance.DESTRUCTIVE)
    left, state = [seconds], {"over": False}

    def words():
        if suspend:
            text = _n("The computer suspends in {n} second.", "The computer suspends in {n} seconds.", left[0])
        else:
            text = _n("The computer shuts down in {n} second.", "The computer shuts down in {n} seconds.", left[0])
        text = text.format(n=left[0])
        # a suspended computer keeps its programs as they are; a switched-off one closes them, and the session does not ask them to save
        return text if suspend else text + "\n\n" + _("Programs that are still open are closed. Unsaved work in them can be lost.")

    def over(go):
        if not state["over"]:
            state["over"] = True
            if go:
                on_go()

    def tick():
        if state["over"]:
            return GLib.SOURCE_REMOVE
        left[0] -= 1
        if left[0] <= 0:
            over(True)  # before the dialog closes: its close would count as Cancel
            dialog.force_close()
            return GLib.SOURCE_REMOVE
        dialog.set_body(words())
        return GLib.SOURCE_CONTINUE

    dialog.set_body(words())
    dialog.connect("response", lambda d, response: over(response == "now"))
    GLib.timeout_add_seconds(1, tick)
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


def speed_dialog(speeds):
    """The result of the speed test: a bar per checksum, fastest first, and a recommendation. Only advice, it changes no setting."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
    box.set_size_request(520, -1)
    box.append(Gtk.Label(label=_("Algorithm speed"), css_classes=["title-3"]))
    box.append(Gtk.Label(label=_("How much data this computer can check per second. A longer bar is faster. Your drive is often slower, and then it sets the pace."),
                         wrap=True, justify=Gtk.Justification.CENTER, css_classes=["dim-label"]))
    notes = {"crc32": _("Only finds accidental errors"), "md5": _("No longer secure"), "sha1": _("No longer secure")}
    card = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
    top = max(speeds.values())
    for key in sorted(speeds, key=speeds.get, reverse=True):
        row = Adw.ActionRow(title=SPEED_NAMES[key], subtitle=notes.get(key, _("Secure")), use_markup=False)
        bar = Gtk.ProgressBar(fraction=speeds[key] / top, valign=Gtk.Align.CENTER, css_classes=["k-speed"] if key in core.SECURE else ["k-speed", "k-weak"])
        bar.set_size_request(170, -1)
        row.add_suffix(bar)
        row.add_suffix(Gtk.Label(label=f"{round(speeds[key] / 1e6)} MB/s", width_chars=9, xalign=1, css_classes=["k-num"]))
        card.append(row)
    box.append(card)
    best, faster = core.speed_advice(speeds)
    lines = [_("<b>{name}</b> is the fastest secure algorithm on this computer.").format(name=SPEED_NAMES[best])]
    if faster == "crc32":
        lines.append(_("CRC32 is faster, but it only finds accidental errors, not deliberate changes."))
    elif faster:
        lines.append(_("{name} is faster, but it is no longer secure against deliberate changes.").format(name=SPEED_NAMES[faster]))
    box.append(Gtk.Label(label=_("Recommendation"), xalign=0, margin_top=6, css_classes=["heading"]))
    box.append(Gtk.Label(label=" ".join(lines), use_markup=True, wrap=True, xalign=0, selectable=False))
    close = Gtk.Button(label=_("Close"), halign=Gtk.Align.END, margin_top=6)
    box.append(close)
    dialog = Adw.Dialog(child=box)
    close.connect("clicked", lambda b: dialog.close())
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


def update_toast(window, version, page):
    """A toast that stays until dismissed, with a button that opens the release page in the browser."""
    toast = Adw.Toast(title=_("TrustCopy {version} is available").format(version=version), button_label=_("Release notes"), timeout=0)
    toast.connect("button-clicked", lambda t: Gtk.UriLauncher(uri=page).launch(window, None, None))
    window.toasts.add_toast(toast)


def weekly_update_check(window):
    """At start, if the user switched the weekly check on and it is due: look in the background, say so only when there is news.
    The date is saved whatever the answer, so an offline start does not retry on every launch."""
    if not desktop.update_due():
        return
    desktop.set_option("update_last", int(time.time()))

    def work():
        try:
            found = desktop.newer_release()
        except (OSError, ValueError, KeyError):
            return
        if found:
            desktop.update_found = found
            GLib.idle_add(update_toast, window, *found)
    threading.Thread(target=work, daemon=True).start()
