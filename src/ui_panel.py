# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DigitGram
"""The job window: one per copy or move, with its graph, bars and file list."""
import math
import os
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

import core
import desktop
from i18n import _, _n
from ui_graph import BLUE, GREEN, draw_badge, draw_curve, draw_grid, draw_title, plot, smoothed, soften
from ui_files import ALL, FilesWindow
from ui_common import AFTER, ALGO_NAMES, Entry, RESULT_ICONS, SUM_NAMES, WHEN, avg_read, avg_write, fmt_duration, outcome, play_sound, power_due, pretty, rate, show_folder, to_paths, when_labels
from ui_dialogs import cleanup_dialog, confirm_always, conflict_dialog, power_dialog, sums_conflict_dialog, sums_gap_dialog


MAX_POINTS = 1200  # the graph keeps the whole job; beyond this every second sample goes, the written total at the others stays exact
CALM = 5  # seconds the graph averages over: a NAS takes data in bursts, over 1 s the curve jumps by a third and over 3 s it still frays; the figures are untouched
BASE_H = 470  # dialogs can only be as big as this window, hence the minimum height


def shown_file(phases, phase, current):
    """The file the window names: the oldest one in flight doing what the job does now (a big file stays named while small ones come and go);
    `current` is only the last one started, which may be finished long ago, so it is the fallback between files."""
    return next((src for src, p in phases.items() if p == phase), next(iter(phases), current))


def no_speed_tip():
    return _("Not enough data for a reliable speed (it takes 1 MiB or more)")


def set_tip(widget, text):
    """Only on a change: draw() runs many times a second."""
    if widget.get_tooltip_text() != text:
        widget.set_tooltip_text(text)


class Panel(Adw.ApplicationWindow):
    """Progress window for one job."""

    def __init__(self, app):
        cfg = desktop.load_config()
        super().__init__(application=app, title="TrustCopy", default_width=640, default_height=BASE_H)
        self.job, self.active, self.closing = None, False, False
        self.paths, self.op, self.paste_dest = [], "copy", None
        self.power_after = ""  # "suspend" or "poweroff" while it waits for the end of the checksum file
        self.sums_reading, self.close_after_sums = False, False  # the read for the checksum file is under way; a window that closes itself waits for it
        self.track, self.marks = [], []  # graph data as (seconds since start, bytes written so far) and failure times
        self.all_written = False  # the graph covers the writing: from here on it stands still
        self.reading, self.saved, self.copy_info = False, None, (0, 0.0)  # step 2: the graph shows the read-back; the copy graph and its (bytes, seconds) wait for the end
        self.verify_secs = 0.0  # how long step 2 took, kept when the job ends: the live value comes from the graph's clock
        self.picked, self.write_data, self.read_data = None, None, None  # after the job: the step shown, and the graphs (track, marks) of writing and reading back
        self.peek = False  # step 1 picked while verifying: the copy graph instead of the live read graph
        self.window = CALM  # seconds the speed is averaged over
        self.lead = None  # seconds before the first written byte: scanning and opening files are no speed, the graph starts after them
        self.ticks, self.speed, self.t0 = 0, 0.0, 0.0
        self.live, self.waited = 0, 0.0  # live: ticks since bytes moved; waited: seconds spent on questions
        self.store, self.entries, self.listed, self.late_seen = Gio.ListStore.new(Entry), {}, 0, 0
        self.keys = {}  # source -> the keys of its rows in `entries`: one, or more when the source is in the job more than once
        self.busy, self.followed = {}, ""  # busy: source -> what its Status cell says now; followed: the file the list scrolled to last
        self.planned = False
        self.last_done, self.last_t, self.seen_failed = 0, 0.0, 0
        self.paused, self.pause_t = 0.0, 0.0  # seconds spent paused, for the average speed
        self.complete = False  # the job ended without cancelling or a fatal error
        self.end = 0.0  # when the job ended: its clock stops there, so the times shown do not grow while the window stays open

        header = self._build_header()
        result, tiles, graph, status, progress = self._build_progress(cfg)
        parts = (self._build_steps(), result, tiles, graph, status, progress, self._build_files_row(cfg), self._build_buttons(cfg))
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=18, margin_start=18, margin_end=18)
        for w in parts:
            body.append(w)
        self.toasts = Adw.ToastOverlay(child=body)
        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(header)
        self.set_content(view)
        self.files_win = FilesWindow(self)
        self.connect("close-request", self.on_close)

    def _build_header(self):
        self.heading = Adw.WindowTitle(title="TrustCopy", subtitle="")
        menu = Gio.Menu()
        other = Gio.Menu()  # a section of its own: GTK draws the line between sections, not between a section and loose items
        other.append(_("Settings"), "app.preferences")
        other.append(_("About TrustCopy"), "app.about")
        menu.append_section(None, other)
        self.route_texts = [Gtk.Label(ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=22, css_classes=["heading"]) for _side in (0, 1)]
        self.t_from, self.t_to = Gtk.Label(css_classes=["dim-label"]), Gtk.Label(label=_("to"), css_classes=["dim-label"])
        run = Gtk.Box(spacing=6)  # the words dim, the folders bold: the eye jumps to the paths
        for w in (self.t_from, self.route_texts[0], self.t_to, self.route_texts[1]):
            run.append(w)
        self.title_stack = Gtk.Stack(hhomogeneous=False, vhomogeneous=False)
        self.title_stack.add_named(self.heading, "done")  # before the job and after it: the plain title or the result
        self.title_stack.add_named(run, "run")
        header = Adw.HeaderBar(title_widget=self.title_stack)
        button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(button)
        return header

    def _build_steps(self):
        """The three steps of a job: copying, verifying, done. Each is a number (a tick when finished) with its title and a line of detail; a line joins one to the next.
        Once the job is over a click on a step shows its graph."""
        row, self.steps, self.joins = Gtk.Box(spacing=12), [], []
        for n, title in enumerate((_("Copying"), _("Verifying"), _("Done")), 1):
            num, detail = Gtk.Stack(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, css_classes=["k-step-num"]), Gtk.Label(xalign=0, css_classes=["dim-label", "caption"], ellipsize=Pango.EllipsizeMode.END)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            name = Gtk.Label(label=title, xalign=0, css_classes=["heading"])
            if n == 1:
                self.first_title = name  # "Moving" for a move
            text.append(name)
            text.append(detail)
            num.add_named(Gtk.Label(label=str(n)), "number")
            num.add_named(Gtk.Image(icon_name="object-select-symbolic"), "tick")  # an icon sits in the middle of the circle, the check mark of a font does not
            item = Gtk.Box(spacing=10, css_classes=["k-step-todo"])
            click = Gtk.GestureClick()
            click.connect("released", lambda *a, n=n: self.show_step(n))
            item.add_controller(click)
            item.append(num)
            item.append(text)
            if n > 1:
                self.joins.append(Gtk.Separator(hexpand=True, valign=Gtk.Align.CENTER))
                row.append(self.joins[-1])
            row.append(item)
            self.steps.append((item, num, detail))
        self.step_row = row
        self.step_rev = Gtk.Revealer(child=row, transition_type=Gtk.RevealerTransitionType.SLIDE_UP, transition_duration=400, reveal_child=True)  # the room it takes is given back once it has faded
        return self.step_rev

    def _fade_steps(self):
        """The job is done and the result says so: the steps have served. They stay a moment, all ticked, then fade out slowly and the room closes behind them.
        The speed tiles still switch the graphs. Without animations (system setting) Adw jumps to the end."""
        def start():
            self.step_row.set_can_target(False)  # a half-gone step must not be clickable
            fade = Adw.TimedAnimation.new(self.step_row, 1.0, 0.0, 1400, Adw.PropertyAnimationTarget.new(self.step_row, "opacity"))
            fade.set_easing(Adw.Easing.EASE_IN_OUT_CUBIC)
            fade.connect("done", lambda *a: self.step_rev.set_reveal_child(False))
            fade.play()
            return GLib.SOURCE_REMOVE
        GLib.timeout_add(900, start)  # the last tick is seen first

    def _build_progress(self, cfg):
        """Everything that shows the running job: percent, state and time left, the bar, the speed graph, the finished step, From/To,
        the summary figures and the result line. Returns the widgets in the order they stand in the window."""
        self.status_icon, self.status_label = Gtk.Image(pixel_size=22), Gtk.Label(xalign=0, wrap=True)
        self.status = Gtk.Box(spacing=10, visible=False)  # a question or an error before the job starts: there only when there is one
        self.status.append(self.status_icon)
        self.status.append(self.status_label)
        self.percent = Gtk.Label(label="0%", valign=Gtk.Align.CENTER, css_classes=["k-pct", "k-num"], can_target=False)  # inside the bar
        self.state = Gtk.Label(ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=30, css_classes=["dim-label", "k-num"])  # the file in flight: grey like the figures beside it, a blue name would look like a link
        self.bytes = Gtk.Label(xalign=0, css_classes=["dim-label", "k-num"])
        self.eta = Gtk.Label(xalign=1, css_classes=["dim-label", "k-num"])
        top = Gtk.CenterBox(start_widget=self.bytes, center_widget=self.state, end_widget=self.eta)
        self.bar = Gtk.ProgressBar(valign=Gtk.Align.CENTER)
        self.bar.add_css_class("k-bar")  # added, not set as a list: that would drop the bar's own "horizontal" class, and with it the theme's height
        self.bar.add_css_class("k-bar-copy")
        self.graph = Gtk.DrawingArea(content_height=110, css_classes=["card"])  # the Overlay around it expands
        self.graph.set_draw_func(self.draw)
        self.big_speed = Gtk.Label(halign=Gtk.Align.END, valign=Gtk.Align.START, margin_top=10, margin_end=14, css_classes=["title-4", "k-num"])
        graph = self.graph_box = Gtk.Overlay(child=self.graph, vexpand=True)
        graph.add_overlay(self.big_speed)
        self.wait_title = Gtk.Label(label=_("Waiting for the previous job…"), css_classes=["title-4"], wrap=True, justify=Gtk.Justification.CENTER)
        self.wait_text = Gtk.Label(css_classes=["dim-label", "k-num"], wrap=True, justify=Gtk.Justification.CENTER)
        self.wait_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER, visible=False)
        for w in (Gtk.Image(icon_name="document-open-recent-symbolic", pixel_size=32, css_classes=["dim-label"]), self.wait_title, self.wait_text):
            self.wait_card.append(w)
        self.wait_cover = Gtk.Box(css_classes=["card"], visible=False)  # hides the empty graph while the job waits
        graph.add_overlay(self.wait_cover)
        graph.add_overlay(self.wait_card)
        self.wait_place, self.wait_timer = (0, 0), 0
        self.top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        bar = Gtk.Overlay(child=self.bar)
        bar.add_overlay(self.percent)
        self.top.append(bar)
        self.top.append(top)
        return self._build_result(), self._build_tiles(), graph, self.status, self.top

    def _build_result(self):
        """What the job came to, there when it is over: a round badge in the colour of the result, what happened and where it went."""
        self.badge = Gtk.Image(pixel_size=32, hexpand=True, vexpand=True)  # fills the circle: left to itself the icon sits at its left edge
        circle = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, hexpand=False, vexpand=False, css_classes=["k-badge"])  # explicit: the icon's expand must not stretch the circle
        circle.append(self.badge)
        self.circle = circle
        self.verdict = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, css_classes=["title-2"])
        self.result = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=6, visible=False)
        self.sums_label = Gtk.Label(css_classes=["dim-label"], ellipsize=Pango.EllipsizeMode.MIDDLE)  # the checksum file: its name, or "Reading checksums… 40 %"
        self.sums_row = Gtk.Box(halign=Gtk.Align.CENTER, visible=False)
        self.sums_row.append(self.sums_label)
        for w in (circle, self.verdict, self.sums_row):
            self.result.append(w)
        return self.result

    def _build_tiles(self):
        """The summary figures as four tiles: size, write and read speed, checksum."""
        self.tiles, self.tile_values, self.tile_boxes = Gtk.Box(spacing=12, homogeneous=True, visible=False), [], []
        for n, title in enumerate((_("Size"), _("Write speed") + " ⌀", _("Read speed") + " ⌀", _("Checksum"))):
            value = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["title-3"])
            tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["k-tile"])
            if n in (1, 2):  # a click on a speed shows its graph
                click = Gtk.GestureClick()
                click.connect("released", lambda *a, n=n: self.show_step(n))  # step 1 is the writing, step 2 the reading
                tile.add_controller(click)
                tile.set_cursor(Gdk.Cursor.new_from_name("pointer"))
            self.tile_boxes.append(tile)
            tile.append(Gtk.Label(label=title, xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["dim-label", "caption"]))
            tile.append(value)
            self.tiles.append(tile)
            self.tile_values.append(value)
        return self.tiles

    def _build_files_row(self, cfg):
        """One quiet row 'Files 128 >' instead of the More/Less button: a click opens the list."""
        self.files_count, self.arrow = Gtk.Label(css_classes=["dim-label"]), Gtk.Image(icon_name="go-next-symbolic")
        box = Gtk.Box(spacing=8)
        for w in (Gtk.Label(label=_("Files"), css_classes=["heading"]), self.files_count, Gtk.Box(hexpand=True), self.arrow):
            box.append(w)
        self.more = Gtk.Button(child=box, css_classes=["flat", "k-files-row"])
        self.more.connect("clicked", self.toggle_files)
        return self.more

    def _build_buttons(self, cfg):
        self.pause = Gtk.Button(label=_("Pause"), visible=False)
        self.cancel = Gtk.Button(label=_("Cancel"))
        self.retry = Gtk.Button(label=_("Retry failed"), visible=False, css_classes=["suggested-action"])
        self.open = Gtk.Button(label=_("Open folder"), visible=False)
        self.open.connect("clicked", self.open_folder)
        self.sweep, self.leftovers = Gtk.Button(visible=False), []  # offered after a job when an earlier, crashed one left unfinished copies in the target folders
        self.sweep.connect("clicked", self.sweep_asked)
        self.pause.connect("clicked", self.toggle_pause)
        self.cancel.connect("clicked", lambda *a: self.close())  # on_close does the right thing in every state
        self.retry.connect("clicked", self.retry_failed)
        self.verify = Adw.SwitchRow(title=_("Verify"), active=cfg["verify"])  # takes effect on the running job
        self.verify.connect("notify::active", lambda row, _p: self.verify_toggled(row))
        self.sums_algo = cfg["sums_algo"]
        self.sums_check = Adw.SwitchRow(active=cfg["sums"])  # takes effect on the running job: the files done before are asked about at the end
        self.sums_check.connect("notify::active", lambda row, _p: self.sums_toggled(row))
        self.name_sums()
        self.after = Adw.ComboRow(title=_("When finished"), model=Gtk.StringList.new(when_labels()), use_subtitle=True)  # the choice under the title: beside it the long ones were cut off
        self.after.set_selected(AFTER.index(cfg["after"]))
        self.after.set_subtitle(when_labels()[self.after.get_selected()])  # use_subtitle only follows a change: the first item (the default) would show none
        confirm_always(self.after, self)
        card = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        for row in (self.verify, self.sums_check, self.after):
            card.append(row)
        self.options_arrow, self.options_summary = Gtk.Image(icon_name="go-next-symbolic"), Gtk.Label(css_classes=["dim-label"], xalign=0, ellipsize=Pango.EllipsizeMode.END, hexpand=True)
        head = Gtk.Box(spacing=8)
        for w in (Gtk.Label(label=_("Options"), css_classes=["heading"]), self.options_summary, self.options_arrow):
            head.append(w)
        self.options_fold, self.size_before_options = card, None  # shown and hidden at once, no slide: see fold_options
        card.set_visible(False)
        toggle = Gtk.Button(child=head, css_classes=["flat", "k-files-row"])  # the look of the Files row
        toggle.connect("clicked", self.fold_options)
        self.options = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)  # one thing to hide when the job is over
        self.options.append(toggle)
        self.options.append(self.options_fold)
        for row in (self.verify, self.sums_check):
            row.connect("notify::active", lambda *a: self.show_options())
        self.show_options()
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        for b in (self.pause, self.sweep, self.retry, self.open, self.cancel):
            buttons.append(b)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        column.append(self.options)
        column.append(buttons)
        return column

    def fold_options(self, *a):
        """Open or shut the card of options. The window grows with it and goes back to the size it had when it is shut again (a window does not
        shrink by itself; not for one the user has maximized or made wider meanwhile: that size is theirs). The speed graph, which takes all the
        room that is left, keeps its height meanwhile and the card comes without sliding in (a Revealer takes its room even without a slide): a slide would give the graph the room before the
        window has grown (it shrinks) and after the card is gone but before the window has shrunk (it swells)."""
        shown = not self.options_fold.get_visible()
        box = self.graph_box
        box.set_size_request(-1, box.get_height())
        box.set_vexpand(False)
        if shown:
            self.size_before_options = (self.get_width(), self.get_height())
        self.options_fold.set_visible(shown)
        self.options_arrow.set_from_icon_name("go-down-symbolic" if shown else "go-next-symbolic")
        if not shown and self.size_before_options:
            width, height = self.size_before_options
            self.size_before_options = None
            if self.get_width() == width and not (self.is_maximized() or self.is_fullscreen()):
                # after the layout without the card: until then the window's least size is still the big one and would refuse
                GLib.timeout_add(50, lambda: (self.set_default_size(width, height), GLib.SOURCE_REMOVE)[1])

        def release():  # the window has its new size: the graph may take the room again, which is the room it has
            box.set_size_request(-1, -1)
            box.set_vexpand(True)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(400, release)

    def show_options(self):
        """What is switched on, beside the fold's title: the state is readable with the card shut."""
        on = [title for title, row in ((_("Verify"), self.verify), (_("Checksum file"), self.sums_check)) if row.get_active()]
        self.options_summary.set_label(", ".join(on))

    def verify_toggled(self, button):
        if self.job:
            self.job.verify = button.get_active()

    def name_sums(self):
        self.sums_check.set_title(_("Checksum file"))
        self.sums_check.set_subtitle(SUM_NAMES[self.sums_algo])

    def sums_toggled(self, button):
        if self.job:
            self.job.sums = self.sums_algo if button.get_active() else None

    def toggle_files(self, *a):
        self.files_win.show_kind(ALL)
        self.files_win.refresh()
        self.files_win.present()
        if self.followed in self.entries:  # opened in the middle of a job: start where it is
            GLib.idle_add(self.follow, self.entries[self.followed])

    # --- getting a job ready -------------------------------------------------
    def say(self, text, kind=""):
        """kind: success / warning / error show the line with an icon, big and in color; without it the line stays dim."""
        self.status_label.set_text(desktop.shown(text))
        self.status.set_visible(bool(text))
        self.status_label.set_css_classes(["title-4", kind] if kind else ["dim-label"])
        self.status_icon.set_visible(bool(kind))
        if kind:
            self.status_icon.set_from_icon_name(RESULT_ICONS[kind])
            self.status_icon.set_css_classes([kind])

    def fail(self, text):
        self.say(text, "error")
        self.cancel.set_label(_("Close"))

    def begin(self, op, paths, dest):
        self.op, self.paths = op, paths
        if not paths:
            return self.fail(_("No files to work on."))
        if dest:
            return self.launch(dest)
        self.say(_("Choose a destination folder…"))
        Gtk.FileDialog(title=_("Copy to…") if op == "copy" else _("Move to…")).select_folder(self, None, self.chosen)

    def chosen(self, dialog, res):
        try:
            folder = dialog.select_folder_finish(res).get_path()
        except GLib.Error:  # dialog dismissed
            folder = None
        if folder:  # None: dismissed, or a place without a local path
            self.launch(folder)
        else:
            self.close()

    def launch(self, dest):
        cfg = desktop.load_config()
        self.start(core.items_for(self.paths, dest), self.op == "move", verify=self.verify.get_active(), policy=cfg["conflict"], algo=cfg["algo"],
                   sums=self.sums_algo if self.sums_check.get_active() else None)

    def start(self, items, move, **options):
        try:
            self.job = core.Job(items, move=move, ask=self.ask, **options)
        except (ValueError, OSError) as e:
            return self.fail(str(e))
        self.verify.set_active(options["verify"])
        if options.get("sums"):  # a retry keeps the kind of its job
            self.sums_algo = options["sums"]
            self.name_sums()
        self.sums_check.set_active(bool(options.get("sums")))
        self.first_title.set_label(_("Moving") if move else _("Copying"))
        self.title_stack.set_visible_child_name("run")
        self.dest = os.path.commonpath([os.path.dirname(dst) for _src, dst in items])
        for label, side in zip(self.route_texts, (0, 1)):
            folder = pretty(os.path.commonpath([os.path.dirname(pair[side]) for pair in items]))
            label.set_text(folder)
            label.set_tooltip_text(folder)
        self.get_application().enqueue(self)

    def paste(self, dest):
        """Files keeps cut/copied files on the clipboard; Wayland only hands them to a focused window."""
        self.say(_("Reading clipboard…"))
        self.paste_dest = dest
        self.connect("notify::is-active", self.read_clipboard)
        GLib.timeout_add(1000, self.read_clipboard)

    def read_clipboard(self, *a):
        dest, self.paste_dest = self.paste_dest, None
        if dest is not None:
            self.get_clipboard().read_async(["x-special/gnome-copied-files", "text/uri-list"], GLib.PRIORITY_DEFAULT, None, self.clipboard_ready, dest)
        return GLib.SOURCE_REMOVE

    def clipboard_ready(self, clipboard, res, dest):
        try:
            stream, _mime = clipboard.read_finish(res)
        except GLib.Error:
            return self.fail(_("The clipboard has no files."))
        out = Gio.MemoryOutputStream.new_resizable()
        flags = Gio.OutputStreamSpliceFlags.CLOSE_SOURCE | Gio.OutputStreamSpliceFlags.CLOSE_TARGET
        out.splice_async(stream, flags, GLib.PRIORITY_DEFAULT, None, self.clipboard_read, dest)

    def clipboard_read(self, out, res, dest):
        try:
            out.splice_finish(res)
        except GLib.Error:
            return self.fail(_("The clipboard has no files."))
        op, uris = core.parse_clipboard(out.steal_as_bytes().get_data().decode(errors="replace"))
        paths = self.local_paths(uris, "/")
        if paths is not None:
            self.begin(op, paths, dest)

    def local_paths(self, names, cwd):
        """The local paths of names. Items without one (trash, recent, some network places) stop the job instead of vanishing."""
        paths = to_paths(names, cwd)
        if len(paths) < sum(bool(n) for n in names):
            self.fail(_("Some selected items are not files on this computer. Nothing was started."))
            return None
        return paths

    # --- running -------------------------------------------------------------
    def show_waiting(self, place, total):
        """A job that has to wait keeps its window, so its options can be set meanwhile; the graph's place says it waits, where, and what runs."""
        self.wait_place = (place, total)
        self.wait_card.set_visible(True)
        self.wait_cover.set_visible(True)
        self._wait_text()
        if not self.wait_timer:
            self.wait_timer = GLib.timeout_add_seconds(1, self._wait_tick)

    def _wait_tick(self):
        if not self.wait_card.get_visible():
            self.wait_timer = 0
            return False
        self._wait_text()
        return True

    def _wait_text(self):
        """"Place 2 of 3 · Copying to /Videos, 45 %": where this job stands and what runs now."""
        place, total = self.wait_place
        parts = [_("Place {pos} of {total}").format(pos=place, total=total)]
        ahead = self.get_application().running
        if ahead and ahead is not self and ahead.job:
            text = _("Moving to {dest}, {percent}%") if ahead.job.move else _("Copying to {dest}, {percent}%")
            parts.append(text.format(dest=ahead.route_texts[1].get_text(), percent=int(ahead.bar.get_fraction() * 100)))
        self.wait_text.set_text(" · ".join(parts))

    def run(self):
        self.active = True
        self.wait_card.set_visible(False)
        self.wait_cover.set_visible(False)
        self.say("")
        self.pause.set_visible(True)
        self.last_t = self.t0 = time.monotonic()
        self.track, self.lead = [(0.0, 0)], None
        threading.Thread(target=self.job.run, daemon=True).start()
        GLib.timeout_add(250, self.tick)
        self.graph.add_tick_callback(self.frame)

    def frame(self, widget, clock):
        if self.job.paused:  # only the "Paused" badge moves between two samples: the curve is redrawn by refresh(), 4 times a second
            widget.queue_draw()
        return self.active

    def toggle_pause(self, *a):
        if self.job.paused:
            self.job.resume()
            self.paused += time.monotonic() - self.pause_t
            self.pause_t = 0.0
            self.pause.set_label(_("Pause"))
        else:
            self.job.pause()
            self.pause_t = time.monotonic()
            self.pause.set_label(_("Resume"))

    def tick(self):
        finished = self.job.finished  # the last tick sees no new bytes: its sample would only dip the curve
        try:
            self.refresh(finished)
        except Exception:  # whatever the view chokes on must not stop the timer: finish() would never run and the queue would stand
            sys.excepthook(*sys.exc_info())
        if finished:
            self.finish()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def refresh(self, finished):
        job, now = self.job, time.monotonic()
        self.live += bool(job.done)
        if not job.paused:  # while paused nothing moves: the speed, and with it the time remaining, stand still
            self.speed = smoothed(self.speed, (job.moved - self.last_done) / max(now - self.last_t, 0.001), self.live)  # everything that moved: writing and checking
        self.last_done, self.last_t = job.moved, now
        frac = job.step_fraction  # each step fills its bar from 0 to 100%

        if job.check_pass and not self.reading:
            self._start_readback(job)
        moved = job.readback if self.reading else job.written
        if not job.paused and not self.all_written and moved and not (self.reading and finished):  # the graph stands still while paused
            if self.lead is None:
                self.lead = self.graph_time() - 0.25  # the first bytes came within the last tick (0.25 s)
            self.track.append((self.graph_time(), moved))
            if len(self.track) > MAX_POINTS:
                self.track = [*self.track[:-1:2], self.track[-1]]
            self.all_written = not self.reading and self.written_share() >= 1
        second = self.ticks % 4 == 0  # the numbers change once per second, the graph on every tick
        self.ticks += 1
        if job.failed > self.seen_failed:
            self.seen_failed = job.failed
            self.marks.append(self.graph_time())  # red line in the graph
        self.graph.queue_draw()
        self.sync_files()

        self._show_bars(job, frac)
        self._show_phase(job, frac)
        self._show_steps(job)
        if second:
            self._show_figures(job)

    def _start_readback(self, job):
        """Step 2 begins: the copy graph is put aside for the end, a new one starts from 0 as the read speed."""
        self.copy_info, self.saved = (job.written, self.graph_time()), (self.track, self.lead, self.marks)
        self.reading, self.track, self.marks = True, [(0.0, 0)], []
        self.lead = (self.lead or 0.0) + self.graph_time()  # its seconds count from now on
        self.all_written = False

    def graph_time(self):
        """Seconds on the graph's axis: the job's clock without the time it stood paused."""
        return self.clock() - self.paused - (time.monotonic() - self.pause_t if self.pause_t else 0) - (self.lead or 0.0)

    def speeds(self, window=1, track=None):
        """The graph's points (seconds, bytes/s) of `track` (default: the one under way): the bytes moved in the last `window` seconds, starting from 0 like a trailing average does.
        shortcut: a job shorter than the window shows a lower hump than its speed, the line carries the average; upgrade if short jobs matter."""
        track, points, j = track or self.track, [], 0
        for i in range(1, len(track)):
            t, written = track[i]
            while j + 1 < i and track[j + 1][0] <= t - window:
                j += 1
            t0, written0 = track[j]
            points.append((t, (written - written0) / max(t - t0, window)))
        return [(0.0, 0.0), *points] if points else []

    def clock(self):
        """Seconds of the job: the time it waited for an answer is not part of it."""
        return (self.end or time.monotonic()) - self.t0 - self.waited

    def written_share(self):
        """How much of the data is written: the graph's x axis, so it is full when the writing is over, whatever the check takes after it."""
        job = self.job
        return min(job.written / job.size, 1) if job.size else 0

    def _show_bars(self, job, frac):
        if job.total:
            self.bar.set_fraction(frac)
            self.percent.set_label(f"{frac:.0%}")
            (self.percent.add_css_class if frac > 0.5 else self.percent.remove_css_class)("k-pct-on")
            kind = "paused" if job.paused else "verify" if job.check_pass else "copy"  # the colour of the step under way, the accent colour while paused
            for name in ("copy", "verify", "paused"):
                (self.bar.add_css_class if name == kind else self.bar.remove_css_class)("k-bar-" + name)
        else:
            self.bar.pulse()

    def _show_steps(self, job):
        """Which step is under way, which are done, and a line about each: size, checksum, time."""
        reached = 4 if self.complete else 2 if job.check_pass else 1  # a cancelled job stays at the step it reached
        checking = job.verify or job.check_pass
        self.steps[1][0].set_visible(checking)  # without verification there are two steps
        self.joins[0].set_visible(checking)
        for i in (1, 2):  # the speed tile of the graph on show is marked
            (self.tile_boxes[i].add_css_class if i == (2 if self.reading else 1) else self.tile_boxes[i].remove_css_class)("k-tile-picked")
        size, secs = self.copy_info if job.check_pass else (job.written, self.graph_time())
        verifying = self.graph_time() if self.active and self.reading else self.verify_secs  # the counter of step 2 runs while it is under way
        details = (fmt_duration(secs) if size else "", f"{ALGO_NAMES[job.algo]} · {fmt_duration(verifying)}" if verifying else ALGO_NAMES[job.algo], fmt_duration(max(self.clock() - self.paused, 0)) if self.complete else "")
        for i, (item, num, detail) in enumerate(self.steps, 1):
            state = "done" if i < reached else "active" if i == reached else "todo"
            item.set_cursor(None if state == "todo" else Gdk.Cursor.new_from_name("pointer"))  # only a step that is reached can be clicked
            item.set_css_classes([f"k-step-{state}", "k-step-item", *(["k-step-picked"] if i == self.picked else [])])
            num.get_child_by_name("number").set_label(str(i - (not checking and i == 3)))  # without verification the last step is the second
            num.set_visible_child_name("tick" if state == "done" else "number")
            detail.set_label(details[i - 1])

    def show_step(self, n):
        """A click on a step shows its graph: the read speed for verifying, the write speed for the others.
        While verifying only the copy graph can be looked at (step 1); any other step goes back to the live one."""
        if (self.active and n > (2 if self.reading else 1)) or (not self.active and n == 3 and not self.complete):
            return  # a step that is not reached has nothing to show
        if self.active:
            if not self.saved:
                return
            self.peek = n == 1
            self.picked = n if self.peek else None
            self._show_info(self.job)
        else:
            data = self.read_data if n == 2 else self.write_data
            if not data:
                return
            self.picked, self.reading = n, n == 2
            self.track, self.marks = data
            self._show_info(self.job)
        self._show_steps(self.job)
        self.graph.queue_draw()

    def _show_phase(self, job, frac):
        """Mark every file in flight with what it is doing, and show the job's phase in the heading and the window title ("Paused" replaces it in the heading)."""
        names = {"scan": _("Scanning…"), "copy": _("Moving") if job.move else _("Copying"), "verify": _("Verifying")}
        flying = dict(job.phases)  # a copy: the workers change it
        current = shown_file(flying, job.phase, job.current)
        self.mark_busy({src: names[phase] for src, phase in flying.items()}, current)
        phase = _("Paused") if job.paused else names[job.phase]
        self.state.set_label(desktop.shown(os.path.basename(current)) if job.phase != "scan" and current and not job.paused else phase)
        self.set_title(f"{frac:.0%} · TrustCopy" if job.total else "TrustCopy")  # visible in the taskbar

    def _show_info(self, job):
        """The title bar's sentence, the big speed and the file count."""
        if job.count:
            sentence = _n("Moving {n} file from", "Moving {n} files from", job.count) if job.move else _n("Copying {n} file from", "Copying {n} files from", job.count)
            self.t_from.set_label(sentence.format(n=job.count))
            speed = self.speed if self.active and not self.peek else (avg_read if self.reading and not self.peek else avg_write)(job)  # a step that is over shows its average, as its line in the graph does
            self.big_speed.set_label(f"{GLib.format_size(int(speed))}/s" if speed > 1024 else "")
            self.files_count.set_label(str(job.count))

    def _show_figures(self, job):
        """The heading's subtitle and the time left; they change once per second."""
        self._show_info(job)
        left = job.step_left  # of the step in view, like the bar: the read-backs of step 2 are not timed at the write speed
        # shortcut: the estimate counts bytes only, so it is far too low for thousands of tiny files; upgrade if that bites
        self.eta.set_label(_("{t} left").format(t=fmt_duration(left / self.speed)) if self.speed > 1024 and job.total else "")
        self.bytes.set_label(_("{a} of {b}").format(a=GLib.format_size(int(job.step_fraction * job.size)), b=GLib.format_size(job.size)) if job.size else "")  # _() outside the f-string: xgettext skips f-strings
        if self.files_win.get_visible():
            self.files_win.refresh()

    def replaced(self, entry, **changes):
        """The entry's successor. Entries are replaced, not edited: the view only rebinds a row when its item changes."""
        new = Entry(entry.src, entry.size, entry.pos, **{"result": entry.result, "busy": entry.busy, "key": entry.key, **changes})
        self.entries[entry.key] = new
        return new

    def update(self, entry, **changes):
        self.store.splice(entry.pos, 1, [self.replaced(entry, **changes)])

    def update_many(self, entries):
        """Puts the new entries into the list, one splice per run of neighbours: a splice per row stalls the window on big lists."""
        rows = sorted({e.pos: e for e in entries}.values(), key=lambda e: e.pos)
        i = 0
        while i < len(rows):
            j = i
            while j + 1 < len(rows) and rows[j + 1].pos == rows[j].pos + 1:
                j += 1
            self.store.splice(rows[i].pos, j - i + 1, rows[i:j + 1])
            i = j + 1

    def add(self, src, size, pos):
        """A new row. A source can be in a job twice (search results: a folder and a file inside it): each gets a row of its own."""
        key = (src, pos) if src in self.entries else src
        entry = self.entries[key] = Entry(src, size, pos, key=key)
        self.keys.setdefault(src, []).append(key)
        return entry

    def row(self, src, result=None):
        """The row of `src`: of several the one that shows `result`, else the first that still waits for one, else the first."""
        rows = [self.entries[key] for key in self.keys.get(src, ())]
        return next((e for e in rows if result is not None and e.result is result), next((e for e in rows if e.result is None), rows[0] if rows else None))

    def sync_files(self):
        """Bring the file list up to date: the plan once it exists, then every new result."""
        job = self.job
        if job.files and not self.planned:  # rows for items refused while scanning are already there: the plan follows them
            self.planned = True
            start = len(self.entries)
            self.store.splice(start, 0, [self.add(src, size, start + i) for i, (src, _dst, _kind, size) in enumerate(job.files)])
        fresh = job.results[self.listed:]
        self.listed += len(fresh)
        changed = []
        for r in fresh:
            entry = self.row(r.src, r)
            if entry is None:  # refused while scanning, so never planned
                entry = self.add(r.src, r.size, self.store.get_n_items())
                self.store.append(entry)
            changed.append(self.replaced(entry, result=r, busy=""))
        self.update_many(changed)
        late = job.late[self.late_seen:]  # files whose verification was switched on afterwards
        self.late_seen += len(late)
        self.update_many([self.replaced(self.row(r.src, r), result=r, busy="") for r in late])

    def mark_busy(self, texts, current=""):
        """Show in each file's Status cell what is happening to it now: `texts` maps the files in flight to it. The list follows `current`."""
        for src in {*self.busy, *texts}:
            entry, text = self.row(src), texts.get(src, "")
            if entry and entry.busy != text and (not text or not entry.result or self.job.check_pass):
                self.update(entry, busy=text)
        self.busy = texts
        entry = self.entries.get(current)
        if entry and current != self.followed:
            self.followed = current
            self.follow(entry)

    def follow(self, entry):
        self.files_win.follow(entry)

    def finish(self):
        job = self.job
        self.listed = 0  # a cancel shortened job.results below what was listed: read them all again, or the latest ones never show
        self.sync_files()  # results that arrived since the last tick
        self.update_many([self.replaced(e, result=None) for e in self.entries.values() if e.result and e.result.status == "copied"])  # a cancel threw those copies away
        self.end = time.monotonic()
        if self.reading:
            self.verify_secs = self.graph_time()
        self.active = False
        self.get_application().next_job()
        bad = [r for r in job.results if r.status == "failed"]
        ok = sum(r.status in ("ok", "verified") for r in job.results)
        self.mark_busy({})
        for w in (self.pause, self.options, self.top, self.status):
            w.set_visible(False)
        self.cancel.set_label(_("Close"))
        self.retry.set_visible(bool(bad) and not job.fatal)
        self.open.set_visible(bool(ok) and not job.fatal)
        self.look_for_leftovers(job)
        if self.pause_t:  # cancelled while paused: that pause still does not count
            self.paused += time.monotonic() - self.pause_t
        self._show_summary(job)
        self.set_title("TrustCopy")
        self.read_data = (self.track, self.marks) if self.saved else None  # the copy graph is what stays, the read graph waits behind step 2
        if self.saved:
            self.track, self.lead, self.marks = self.saved
            self.reading, self.all_written = False, True
        self.write_data, self.peek = (self.track, self.marks), False
        self._show_info(job)  # the graph on show decides the figure: after the restore above it is the copy graph; no figure when the step has no speed (see rate)
        if not (job.cancelled or job.fatal):
            self.complete = True
            self.picked = 3  # the steps stay, all ticked: a click on one shows its graph, a click on "Done" the copy graph again
        self._show_steps(job)
        if self.complete:
            self._fade_steps()
        title, text, kind = outcome(job, ok, bad)
        self.heading.set_title(title)
        # the total time, which the "Done" step showed before it faded: under the title, "Done" / "in 2 min 13 s"
        self.heading.set_subtitle(_("in {t}").format(t=fmt_duration(max(self.clock() - self.paused, 0))) if self.complete else "")
        self.title_stack.set_visible_child_name("done")
        self.verdict.set_label(desktop.shown(text))
        self.badge.set_from_icon_name(RESULT_ICONS[kind])
        self.circle.set_css_classes(["k-badge", kind])
        self.result.set_visible(True)
        self.tiles.set_visible(True)
        self.files_win.refresh()
        after = WHEN[self.after.get_selected()]
        closes = self.closing or after == "always" or (after == "clean" and kind == "success")
        self.announce(title, text, kind, folder=self.open.get_visible() and not closes)  # a window that closes takes the app along: the button would lead nowhere
        app = self.get_application()
        self.power_after = power_due(after, kind, bool(app.running or app.waiting))
        waiting = self.offer_sums(job)  # always asked: `closes and ...` would not call it when the window stays
        self.close_after_sums = closes and waiting
        if closes and not waiting:
            self.close()
        elif self.power_after and not waiting:
            self.confirm_power()

    # --- the checksum file ------------------------------------------------------
    def offer_sums(self, job):
        """After the job: the checksum file. The files that have no checksum (done before it was switched on, or only renamed) are asked about first.
        Returns whether it goes on in the background, so a window that closes itself waits for it."""
        if not job.sums or job.cancelled or job.fatal or not job.sum_files():
            return False
        gap, size = job.sum_gap()
        if gap:
            sums_gap_dialog(len(gap), len(job.sum_files()), size, self.sums_gap_answered).present(self)
        else:
            self.collect_sums(False)
        return True

    def sums_gap_answered(self, choice):
        if choice == "none":
            self.sums_done()
        else:
            self.collect_sums(choice == "all")

    def collect_sums(self, read_gap):
        job, self.sums_reading = self.job, read_gap
        if read_gap:
            self.sums_row.set_visible(True)
            GLib.timeout_add(250, self.sums_progress, job.sum_gap()[1])

        def work():
            entries = job.sum_entries(read_gap)
            GLib.idle_add(self.sums_collected, entries)

        threading.Thread(target=work, daemon=True).start()

    def sums_progress(self, total):
        if not self.sums_reading:  # the last tick after the end must not overwrite the result
            return False
        self.sums_label.set_label(_("Reading checksums… {percent} %").format(percent=min(100, int(100 * self.job.sum_read / max(total, 1)))))
        return self.sums_reading

    def sums_collected(self, entries):
        self.sums_reading = False
        job = self.job
        if not entries:
            return self.sums_done()
        existing = job.sum_existing()
        if existing:
            path, found, mtime = existing
            sums_conflict_dialog(path, found, mtime, len(entries), lambda how: self.write_sums(entries, how)).present(self)
        else:
            self.write_sums(entries, "keep_both")
        return GLib.SOURCE_REMOVE

    def write_sums(self, entries, how):
        if how == "skip":
            return self.sums_done()
        try:
            self.sums_done(self.job.write_sums(entries, how))
        except OSError as e:
            self.sums_done(error=e.strerror or str(e))

    def sums_done(self, path="", error=""):
        """The row under the result: that the file is made and its name (the folder button below opens where it lies), or why there is none;
        nothing when none was written on purpose."""
        self.sums_row.set_visible(bool(path or error))
        if path:
            self.sums_label.set_label(_("Checksum file created: {name}").format(name=desktop.shown(os.path.basename(path))))
        elif error:
            self.sums_label.set_label(_("The checksum file could not be written: {error}").format(error=desktop.shown(error)))
        if self.close_after_sums:
            self.close()
        elif self.power_after:
            self.confirm_power()

    def confirm_power(self):
        """Switch the computer off or put it to sleep, as chosen under "When finished": after a countdown that can be cancelled."""
        action, self.power_after = self.power_after, ""

        def go():
            app = self.get_application()
            if app.running or app.waiting:  # another job started during the countdown: its window decides when the computer goes off
                return
            if error := desktop.power(action):
                text = _("The computer could not be suspended: {error}") if action == "suspend" else _("The computer could not be shut down: {error}")
                alert = Adw.AlertDialog.new(None, text.format(error=desktop.shown(error)))
                alert.add_response("close", _("Close"))
                alert.present(self)

        power_dialog(action, go).present(self)

    def _show_summary(self, job):
        """The tiles: size, average speeds and checksum of the job."""
        copied = sum(r.size for r in job.results if r.status in ("ok", "verified"))
        values = (GLib.format_size(copied), rate(job.written, job.writing), rate(job.readback, job.reading), ALGO_NAMES[job.algo] if any(r.digest for r in job.results) else "–")
        for label, text, tile in zip(self.tile_values, values, self.tile_boxes):
            label.set_label(text)
            if text == "–" and tile is not self.tile_boxes[3]:  # a speed that was not measured: say why
                tile.set_tooltip_text(no_speed_tip())

    def open_folder(self, *a):
        show_folder(Gio.File.new_for_path(self.dest), self)

    def announce(self, title, text, kind, folder=False):
        """A notification when nobody is looking at the window, and the sound if wanted. With `folder` it offers to open the target."""
        cfg, app = desktop.load_config(), self.get_application()
        if cfg["notify"] and not self.is_active():
            note = Gio.Notification.new(title)
            note.set_body(desktop.shown(text))  # a fatal error may name a path that is no UTF-8
            if folder:
                note.add_button_with_target(_("Open folder"), "app.open-folder", GLib.Variant("s", Gio.File.new_for_path(self.dest).get_uri()))  # the URI is plain ASCII, a path need not be UTF-8
            app.send_notification(f"done-{id(self)}", note)
        if cfg["sound"]:
            play_sound(app, kind)

    def look_for_leftovers(self, job):
        """After a job: unfinished copies (name.trustpart) in the folders it wrote to come from an earlier job that stopped suddenly. Only when no
        other job runs or waits, whose own temp files look just the same. In a thread: a slow network folder must not hold the window."""
        app = self.get_application()
        if app.running or app.waiting:
            return
        folders = {os.path.dirname(r.dst) for r in job.results} | {self.dest}

        def scan():
            if found := core.find_leftovers(folders):
                GLib.idle_add(self.offer_sweep, found)

        threading.Thread(target=scan, daemon=True).start()

    def offer_sweep(self, found):
        if self.is_visible():  # a window that closed itself meanwhile has nobody to ask
            self.leftovers = found
            self.sweep.set_label(_n("Delete {n} unfinished copy…", "Delete {n} unfinished copies…", len(found)).format(n=len(found)))
            self.sweep.set_sensitive(True)
            self.sweep.set_visible(True)
        return GLib.SOURCE_REMOVE

    def sweep_asked(self, *a):
        cleanup_dialog(self.leftovers, self.sweep_answered).present(self)

    def sweep_answered(self, yes):
        if not yes:
            return
        gone, errors = core.remove_leftovers([path for path, _size in self.leftovers])
        if errors:  # what could not go stays offered
            self.leftovers = [(path, 0) for path, _error in errors]
            self.offer_sweep(self.leftovers)
            self.sweep.set_tooltip_text(desktop.shown("\n".join(f"{path}: {error}" for path, error in errors[:8])))
        else:
            self.sweep.set_label(_n("{n} file deleted", "{n} files deleted", gone).format(n=gone))  # the button stays as the answer, greyed out
            self.sweep.set_sensitive(False)

    def retry_failed(self, *a):
        job = self.job
        panel = Panel(self.get_application())
        panel.present()
        panel.start([(r.src, r.dst) for r in job.results if r.status == "failed"], job.move, verify=job.verify, policy=job.policy, algo=job.algo, sums=job.sums,
                    replace={r.dst for r in job.results if r.kept}, dirs=job.dirs)
        self.retry.set_visible(False)

    def on_close(self, *a):
        if self.active:  # let the engine remove its temp file first, finish() closes the window afterwards
            self.job.cancel()
            self.closing = True
            self.say(_("Cancelling…"))
            return True
        self.get_application().forget(self)
        self.files_win.destroy()  # a hidden window would keep the app running
        return False

    # --- conflicts (called from a worker thread) -------------------------------
    def ask(self, src, dst):
        """Shows the conflict dialog and waits for the answer: (policy, apply to all)."""
        answer, done = [], threading.Event()

        def respond(key, remember):
            if not done.is_set():  # the first way out counts: a click closes the dialog, which reports "skip" as well
                answer.append((key, remember and key != "cancel"))
                done.set()

        def show():
            try:
                conflict_dialog(src, dst, respond).present(self)
            except Exception:  # e.g. a file that vanished: without an answer the worker would wait for ever
                respond("skip", False)
                raise

        GLib.idle_add(show)
        since = time.monotonic()
        while not done.wait(0.2):
            if self.job.cancelled:
                return ("cancel", False)
            since = self.count_wait(since)
        return answer[0]

    def count_wait(self, since):
        """A question with nothing else in flight stops the job's clock (duration and graph). Returns the time now.
        shortcut: another file in flight keeps the clock running, though the question may hold the rest back."""
        now = time.monotonic()
        if not self.job.phases:
            self.waited += now - since
        return now

    # --- speed graph: the x axis is the elapsed time, so the curve fills the width; it is the writing only ----
    def draw(self, area, cr, w, h):
        fg, job = area.get_color(), self.job
        set_tip(area, None)
        track, marks, reading = (self.saved[0], self.saved[2], False) if self.peek else (self.track, self.marks, self.reading)
        h = draw_title(cr, fg, h, ((_("Moving") if job.move else _("Copying")) + " · " if self.peek else "") + (_("Read speed") if reading else _("Write speed")))  # looking at step 1 while step 2 runs: say which graph this is
        draw_grid(cr, fg, w, h)
        if job and not self.active and job.move and not job.written and any(r.status == "ok" for r in job.results):
            return draw_badge(cr, fg, w, h, [_("Moved instantly"), _("same partition, nothing to copy")])  # renamed, not copied: there is no curve to show
        samples = soften(self.speeds(self.window, track))  # the mean over the window, then softened: a burst is a hump, not a step
        avg = (avg_read(job) if reading else avg_write(job)) if job and (not self.active or self.peek) else 0
        secs = (job.reading if reading else job.writing).secs if avg else 0
        if avg and (len(samples) < 2 or secs <= self.window):  # over within the first window (a fast disk, a few photos): the samples are only a low hump, so its average, flat
            samples = [(0.0, avg), (max(secs, 0.05), avg)]  # 0.05: plot's shortest span, so it fills the width
        if len(samples) < 2 and not avg and job and not self.active and (job.readback if reading else job.written):
            set_tip(area, no_speed_tip())
            return draw_curve(cr, [(0, h / 2), (w, h / 2)], h, BLUE if reading else GREEN)  # under 1 MiB no speed is honest (see rate): done, without a figure
        if len(samples) < 2:
            return
        last = avg or samples[-1][1]  # the line is the last speed, and for a step that is over its average, the figure the summary shows
        live = self.active and not self.peek
        plot(cr, fg, w, h, samples, BLUE if reading else GREEN, last, marks, 1 if live else 0.05)
        if live and job.paused:
            draw_badge(cr, fg, w, h, [_("Paused")], 0.65 + 0.3 * math.sin(time.monotonic() * 4))
