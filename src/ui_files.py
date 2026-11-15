# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DigitGram
"""The file list in a window of its own, in the style of HashTally's detail window: filter tabs with counts, a coloured status,
the sums in the footer. It reads the job window's store, so it fills in while the job runs. A Gtk.ColumnView only draws
the lines in sight, so thousands of files are no trouble."""
import os
from collections import Counter

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Pango

import desktop
from i18n import _
from ui_common import ALGO_NAMES, STATUS, show_folder

ALL = "all"
KINDS = {"verified": "verified", "ok": "done", "skipped": "skipped", "failed": "failed"}  # Result.status -> tab; the rest is still to do or under way
LOOK = {"k-ok": "success", "k-bad": "error", "k-warn": "warning", "k-done": "k-state-verify", "k-info": "dim-label", "k-wait": "dim-label"}  # the status pill's class -> the colour of its word


def kind_of(entry):
    """The tab an entry belongs to besides "All"; None while it is waiting or under way."""
    return KINDS.get(entry.result.status) if entry.result else None


class FilesWindow(Adw.Window):
    def __init__(self, panel):
        super().__init__(title=_("Files"), default_width=860, default_height=480)  # on its own, not transient: only a window of its own has minimize and maximize
        self.panel, self.kind, self.hover = panel, ALL, False  # hover: the list does not follow the job while the pointer is over it, so scrolling by hand works
        self.set_hide_on_close(True)
        self.filter = Gtk.CustomFilter.new(lambda entry: self.kind == ALL or kind_of(entry) == self.kind)
        self.table = Gtk.ColumnView(model=Gtk.NoSelection(model=Gtk.FilterListModel(model=panel.store, filter=self.filter)),
                                    show_row_separators=True, css_classes=["data-table", "k-table"])
        self.sums = []  # the two checksum columns: their titles name the algorithm
        for title, bind, xalign, props in ((_("Status"), self._status, 0.0, {"fixed_width": self._status_width()}), (_("Source file"), self._file, 0.0, {"expand": True}),
                                           (_("Size"), self._size, 1.0, {"fixed_width": 90}), (_("Source checksum"), self._source, 0.0, {"fixed_width": 190}),
                                           (_("Target checksum"), self._target, 0.0, {"fixed_width": 190})):
            factory = Gtk.SignalListItemFactory()
            factory.connect("setup", self._setup, xalign)
            factory.connect("bind", lambda f, item, bind=bind: self._bound(item, bind))
            column = Gtk.ColumnViewColumn(title=title, factory=factory, resizable=True, **props)
            if bind in (self._source, self._target):
                self.sums.append((column, title))
            self.table.append_column(column)
        scroll = Gtk.ScrolledWindow(child=self.table)
        pointer = Gtk.EventControllerMotion()
        pointer.connect("enter", lambda *a: setattr(self, "hover", True))
        pointer.connect("leave", lambda *a: setattr(self, "hover", False))
        scroll.add_controller(pointer)

        self.buttons, self.tab_labels, first = {}, {}, None
        switch = Gtk.Box(css_classes=["linked"])
        for key, label, dot in ((ALL, _("All"), None), ("verified", _("Verified"), "success"), ("done", _("Done"), "k-state-verify"), ("skipped", _("Skipped"), "warning"), ("failed", _("Failed"), "error")):
            button, content = Gtk.ToggleButton(group=first, active=key == ALL), Gtk.Box(spacing=6)
            first = first or button
            if dot:
                content.append(Gtk.Label(label="●", css_classes=[dot]))
            self.tab_labels[key] = Gtk.Label()
            self.tab_labels[key].words = label
            content.append(self.tab_labels[key])
            button.set_child(content)
            button.connect("toggled", lambda b, key=key: b.get_active() and self.show_kind(key))
            self.buttons[key] = button
            switch.append(button)

        self.summary = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END, css_classes=["dim-label"])
        footer = Gtk.Box(margin_start=12, margin_end=12, margin_top=8, margin_bottom=8, spacing=12)
        footer.append(self.summary)

        page = Adw.ToolbarView(content=scroll)
        page.add_top_bar(Adw.HeaderBar(title_widget=switch))
        page.add_bottom_bar(footer)
        self.set_content(page)
        self.refresh()

    @staticmethod
    def _status_width():
        """As wide as the longest status word in this language, so none is cut off: a dot, the word, the cell's margins."""
        probe, widths = Gtk.Label(), []
        for word in (*(_(label) for _icon, _css, label, _tint in STATUS.values()), _("Copying"), _("Moving"), _("Verifying"), _("Scanning…"), _("Waiting")):
            probe.set_label("●  " + word)
            widths.append(probe.measure(Gtk.Orientation.HORIZONTAL, -1)[1])
        return max(widths) + 32

    def show_kind(self, kind):
        self.kind = kind
        self.buttons[kind].set_active(True)  # also when called from outside: opening the window always starts on "All"
        self.filter.changed(Gtk.FilterChange.DIFFERENT)

    def refresh(self):
        """The tabs' counts and the footer; the job window calls it once a second and when the job ends."""
        p, entries = self.panel, self.panel.entries.values()
        counts = Counter(kind_of(e) for e in entries)
        counts[ALL] = len(p.entries)
        for key, label in self.tab_labels.items():
            label.set_label(f"{label.words} {counts[key]}")
        job = getattr(p, "job", None)
        algo = ALGO_NAMES.get(job.algo) if job and (job.verify or job.check_pass) else None  # the steps fade out at the end, so the algorithm is named here as well
        for column, title in self.sums:
            column.set_title(f"{title} ({algo})" if algo else title)
        verify = getattr(p, "verify", None)  # the checkbox: not there yet while the panel is still being built; it may be switched while the job runs
        always = (ALL, "verified" if verify is None or verify.get_active() else "done")  # the tab of the good result the setting makes shows even when empty
        for key, button in self.buttons.items():  # the others show once they hold something; the one you are on stays
            button.set_visible(key in always or bool(counts[key]) or self.kind == key)
        self.summary.set_label(" · ".join(t for t in (p.bytes.get_label(), p.eta.get_label()) if t) if p.active else p.verdict.get_label())

    def follow(self, entry):
        """Scroll so that the file in progress is the second last row: the one after it shows too. Only the unfiltered list follows."""
        if self.get_visible() and self.kind == ALL and not self.hover:
            self.table.scroll_to(min(entry.pos + 1, len(self.panel.store) - 1), None, Gtk.ListScrollFlags.NONE, None)

    # -- the cells
    def _setup(self, _factory, item, xalign):
        label = Gtk.Label(xalign=xalign, hexpand=True, margin_start=10, margin_end=10, margin_top=9, margin_bottom=9, ellipsize=Pango.EllipsizeMode.MIDDLE)
        click = Gtk.GestureClick(button=3)  # a right click: look at the file in its folder
        click.connect("pressed", lambda g, n, x, y: self._menu(label, label.entry))
        label.add_controller(click)
        cell = Gtk.Box()  # fills the cell and carries the tint of its row
        cell.append(label)
        item.set_child(cell)

    def _bound(self, item, bind):
        cell, entry = item.get_child(), item.get_item()
        label = cell.get_first_child()
        label.entry = entry
        text, css, tip = bind(entry)
        label.set_label(text)
        label.set_css_classes(css)
        label.set_tooltip_text(tip or None)
        tint = entry.status()[3]
        cell.set_css_classes([tint] if tint else [])  # only the deviations are tinted: what was verified stays in the background

    @staticmethod
    def _status(entry):
        _icon, css, text, _tint, tip = entry.status()
        if entry.busy:  # under way: the colour of its step in the job window, copying green and verifying blue
            css = "k-state-verify" if entry.busy == _("Verifying") else "k-state-copy" if entry.busy in (_("Copying"), _("Moving")) else "dim-label"
        return f"●  {text}", [css if entry.busy else LOOK.get(css, "dim-label")], tip  # the tip of a failed file is the reason

    @staticmethod
    def _file(entry):
        name, kind = desktop.shown(os.path.basename(entry.src)), kind_of(entry)
        return name, ["dim-label"] if kind == "verified" else ["heading"] if kind in ("skipped", "failed") else [], desktop.shown(entry.src)

    @staticmethod
    def _source(entry):
        return FilesWindow._sum(entry, entry.result.source if entry.result else "", "dim-label")

    @staticmethod
    def _target(entry):
        return FilesWindow._sum(entry, entry.result.target if entry.result else "", "error")  # a bad copy: what was found is red, as in HashTally

    @staticmethod
    def _sum(entry, digest, bad):
        kind = kind_of(entry)
        return (f"{digest[:10]}…{digest[-10:]}" if len(digest) > 24 else digest or "–"), ["k-hash", bad if kind == "failed" and digest else "dim-label" if kind == "verified" or not digest else ""], digest  # the ends are what the eye compares

    @staticmethod
    def _size(entry):
        return GLib.format_size(entry.size), ["numeric", *(["dim-label"] if kind_of(entry) == "verified" else [])], None

    def _menu(self, widget, entry):
        popover, box = Gtk.Popover(has_arrow=False), Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        popover.set_child(box)
        popover.set_parent(widget)
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        offers = [(_("Show in folder"), lambda: show_folder(Gio.File.new_for_path(os.path.dirname(entry.src)), self))]
        if (r := entry.result) and r.source:  # as in HashTally: both entries whenever there are sums, so the menu does not change with the result
            copy = lambda text: (lambda: self.get_clipboard().set(text))
            offers[:0] = [(_("Copy source checksum"), copy(r.source)), (_("Copy target checksum"), copy(r.target))]
        for label, act in offers:
            button = Gtk.Button(label=label, css_classes=["flat"])
            button.connect("clicked", lambda b, act=act: (popover.popdown(), act()))
            box.append(button)
        popover.popup()
