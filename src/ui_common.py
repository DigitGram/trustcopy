# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DigitGram
"""What the windows share: constants, small formatters, the file list's columns and the result summary."""
import os
import re
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

import core
import desktop
from i18n import N_, _, _n


APP_ID = "io.github.digitgram.TrustCopy"
RESULT_ICONS = {"success": "object-select-symbolic", "warning": "dialog-warning-symbolic", "error": "dialog-error-symbolic"}  # as in HashTally
CONFLICTS, AFTER = desktop.CONFLICTS, desktop.AFTER
WHEN = AFTER + ("suspend", "poweroff")  # the job window also offers to switch the computer off; the setting does not: that default would bite someday
ALGOS = tuple(core.ALGOS)
SUM_NAMES = {"sha256": "SHA-256", "sha512": "SHA-512", "sha1": "SHA-1", "md5": "MD5", "crc32": "SFV"}  # the kinds of checksum file, as the window names them
ALGO_NAMES = {"crc32": "CRC32", "blake2b": "BLAKE2b", "sha256": "SHA-256"}
STATUS = {  # Result.status: (icon, pill class, label, row tint)
    "verified": ("object-select-symbolic", "k-ok", N_("Verified"), ""),  # the usual case: only its word is green, the deviations stand out
    "copied": (None, "k-info", N_("Copied"), ""),  # written to its temp file, waiting for the check that gives it its name
    "ok": (None, "k-done", N_("Done"), ""),  # copied without verification: the usual case with Verify off; blue, not green: nothing was verified
    "skipped": ("media-skip-forward-symbolic", "k-warn", N_("Skipped"), "k-row-warn"),
    "failed": ("window-close-symbolic", "k-bad", N_("Failed"), "k-row-bad"),
}
# The row tints use libadwaita's named colors, so they follow light/dark mode (same look as HashTally).
CSS = """
/* easter egg cat: a popover that shows nothing but the drawing */
popover.k-cat > contents { background: none; border: none; box-shadow: none; padding: 0; }
.k-step-num { min-width: 28px; min-height: 28px; border-radius: 99px; font-weight: bold; background: alpha(@window_fg_color, 0.1); }
.k-step-picked { background: alpha(@window_fg_color, 0.07); border-radius: 12px; }
.k-step-item { padding: 4px 10px; }
.k-step-active .k-step-num { background: @accent_bg_color; color: @accent_fg_color; }
.k-step-done .k-step-num { background: @success_bg_color; color: @success_fg_color; }
.k-step-todo { opacity: 0.55; }
.k-bar trough, .k-bar progress { min-height: 18px; }
.k-pct { font-weight: bold; font-size: 1.1em; line-height: 1; padding: 0; text-shadow: 0 0 3px alpha(@window_bg_color, 0.6); }  /* the percent inside the bar */
.k-pct-on { color: white; text-shadow: 0 0 3px rgba(0, 0, 0, 0.5); }  /* once the fill reaches it */
.k-bar-copy progress { background: @green_5; }  /* the step colors of the graph: copying green, verifying blue */
.k-bar-verify progress { background: @blue_3; }
.k-bar-paused progress { background: @accent_bg_color; }  /* paused: the system's accent colour (orange on Ubuntu) */
.k-num { font-feature-settings: "tnum"; }
.k-files-row { padding: 10px 14px; border-radius: 14px; background: alpha(@window_fg_color, 0.06); }
.k-state-copy { color: mix(@green_5, @window_fg_color, 0.2); }
.k-state-verify { color: mix(@blue_3, @window_fg_color, 0.2); }
.k-badge { min-width: 64px; min-height: 64px; border-radius: 99px; background: alpha(currentColor, 0.15); }
/* the tile of the graph on show */
.k-tile-picked { box-shadow: inset 0 0 0 2px alpha(@window_fg_color, 0.3); }
.k-tile { padding: 10px 14px; border-radius: 14px; background: alpha(@window_fg_color, 0.06); }
.k-slim row { min-height: 36px; }  /* a card of file names: five rows must fit under the heading in the small job window */
.k-table cell { padding: 0; }
.k-hash { font-family: monospace; font-size: 0.9em; }
.k-row-bad  { background: alpha(@error_bg_color, 0.13); }
.k-row-warn { background: alpha(@warning_bg_color, 0.16); }
"""


def to_paths(args, cwd):
    """Accept plain paths or URIs (Files sends URIs) and return local paths."""
    files = (Gio.File.new_for_commandline_arg_and_cwd(a, cwd) for a in args if a)
    return [p for p in (f.get_path() for f in files) if p]


def show_folder(folder, parent=None):
    """Show a Gio.File in Files; a GVfs mount path becomes its smb:// URI here."""
    try:  # the FileManager1 service of Files handles network locations; the generic launcher finds no app for smb://
        Gio.DBusProxy.new_for_bus_sync(Gio.BusType.SESSION, 0, None, "org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
                                       "org.freedesktop.FileManager1", None).call_sync(
            "ShowFolders", GLib.Variant("(ass)", ([folder.get_uri()], "")), 0, -1, None)
    except GLib.Error:
        Gtk.FileLauncher.new(folder).launch(parent, None, lambda l, res: l.launch_finish(res))


def leftover_rows(found, limit=4, hint=20):
    """What the clean-up dialog lists, from (path, size) pairs: (the folder when they all lie in one, else None; rows of (name, folder to show beside
    the name, size, real path); None or (how many more, their size, the next `hint` names for a tooltip)). Beyond `limit` (+1: "and 1 more" never
    stands for one file) the rest is only counted: a thousand leftovers stay five rows."""
    shown = found if len(found) <= limit + 1 else found[:limit]  # a last row that says "1 more" would take the room of that one file
    folders = {os.path.dirname(p) for p, _size in found}
    folder = next(iter(folders)) if len(folders) == 1 else None
    rows = [(os.path.basename(p), None if folder else pretty(os.path.dirname(p)), size, p) for p, size in shown]
    rest = found[len(shown):]
    names = [os.path.basename(p) if folder else pretty(p) for p, _size in rest[:hint]]
    more = (len(rest), sum(size for _p, size in rest), "\n".join(names) + ("\n…" if len(rest) > hint else "")) if rest else None
    return folder, rows, more


def pretty(path):
    """Readable location: ~ for the home folder, smb://server/share/... for mounted network shares."""
    name = Gio.File.new_for_path(path).get_parse_name()  # GVfs maps its FUSE paths back to the URI Files shows
    m = re.match(r"/run/user/\d+/gvfs/smb-share:server=([^,/]+),share=([^,/]+)[^/]*(.*)", name)  # fallback if it did not
    if m:
        name = f"smb://{m[1]}/{m[2]}{m[3]}"
    home = os.path.expanduser("~")
    return "~" + name[len(home):] if name == home or name.startswith(home + "/") else name


def fmt_duration(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h} h {m} min"  # units are the same in every language
    return f"{m} min {s} s" if m else f"{s} s"


def describe(path):
    """Size and modification time of `path`, for the conflict dialog."""
    st = os.lstat(path)  # lstat: a dangling symlink must not break the dialog
    return f"{GLib.format_size(st.st_size)} · {datetime.fromtimestamp(st.st_mtime):%x %X}"


def avg(nbytes, clock):
    """Average speed in bytes/s over the I/O seconds in `clock`; 0 when too little was moved to tell (see rate)."""
    return nbytes / clock.secs if nbytes >= 1 << 20 and clock.secs > 0.001 else 0.0


def avg_write(job):
    return avg(job.written, job.writing)


def avg_read(job):
    return avg(job.readback, job.reading)


def rate(nbytes, clock):
    """Average speed of the I/O seconds in `clock`; too little written or read (a rename, no verification, only small files): no speed."""
    # below 1 MiB the seconds are mostly latency (a few KB on SMB take 0.1 s: "29 KB/s", and the figure stands still while a big file is written)
    return f"{GLib.format_size(int(speed))}/s" if (speed := avg(nbytes, clock)) else "–"


def key_value_grid(*keys):
    """Grid of 'key  value' lines; returns the grid and the value labels."""
    grid, values = Gtk.Grid(column_spacing=12, row_spacing=4), []
    for i, key in enumerate(keys):
        value = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=50)
        grid.attach(Gtk.Label(label=key, xalign=0, css_classes=["dim-label"]), 0, i, 1, 1)
        grid.attach(value, 1, i, 1, 1)
        values.append(value)
    return grid, values


class Entry(GObject.Object):
    """One line of the file list: a planned file and, once it is done, its result."""

    def __init__(self, src, size, pos, result=None, busy="", key=None):
        super().__init__()
        self.src, self.size, self.pos, self.result = src, size, pos, result
        self.key = key or src  # its key in the job window's `entries`: the source, or (source, row) when the source is in the job once more
        self.busy = busy  # what is happening right now ("Copying", "Verifying")

    def status(self):
        """(icon, pill class, text, row tint, tooltip) for the current state."""
        r = self.result
        if self.busy or r is None:
            return None, "k-wait", self.busy or _("Waiting"), "", ""
        icon, css, label, tint = STATUS[r.status]
        return icon, css, _(label), tint, r.error  # the error is the row's tooltip: a pill with it would be far too wide


def outcome(job, ok, bad):
    """(title, text, kind) of the result line for a finished job. `ok` counts the files done, `bad` lists the failed results."""
    if job.fatal:
        title, text, kind = _("Stopped"), job.fatal, "error"
    elif job.cancelled:
        title, kind = _("Cancelled"), "warning"
        text = _n("{n} file was done before cancelling.", "{n} files were done before cancelling.", ok).format(n=ok)
    elif bad:
        title, kind = _("Finished with errors"), "error"
        text = _n("{ok} file done, {bad} failed.", "{ok} files done, {bad} failed.", ok).format(ok=ok, bad=len(bad))
    elif not ok and job.results:  # every file was skipped: not a success, so no "done" sound and the window stays
        skipped = len(job.results)
        title, kind = _("Skipped"), "warning"
        text = _n("{n} file skipped", "{n} files skipped", skipped).format(n=skipped)
    else:
        title, kind = _("Done"), "success"
        checked = bool(ok) and all(r.status == "verified" for r in job.results if r.status in ("ok", "verified"))  # a plain rename is not, nor is an empty job
        if job.move:
            text = (_n("{n} file moved and verified successfully", "{n} files moved and verified successfully", ok) if checked
                    else _n("{n} file moved successfully", "{n} files moved successfully", ok))
        else:
            text = (_n("{n} file copied and verified successfully", "{n} files copied and verified successfully", ok) if checked
                    else _n("{n} file copied successfully", "{n} files copied successfully", ok))
        text = text.format(n=ok)
        if skipped := sum(r.status == "skipped" for r in job.results):  # files left behind: no clean success, so "Close on success" keeps the window
            text, kind = text + " · " + _n("{n} file skipped", "{n} files skipped", skipped).format(n=skipped), "warning"
    return title, text, kind


def play_sound(app, kind):
    """The sound theme's "complete", or "dialog-warning" when something went wrong; the bell if the theme is missing."""
    path = f"/usr/share/sounds/freedesktop/stereo/{'complete' if kind == 'success' else 'dialog-warning'}.oga"
    if not os.path.exists(path):
        return Gdk.Display.get_default().beep()
    app.sound = Gtk.MediaFile.new_for_filename(path)  # kept on the app: a garbage-collected player stops
    app.sound.play()
    app.hold()  # the app must outlive the sound when the last window just closed
    GLib.timeout_add_seconds(3, app.release)


def after_labels():
    return [_("Keep open"), _("Close on success"), _("Always close")]


def when_labels():
    return after_labels() + [_("Suspend computer"), _("Shut down computer")]


def power_due(after, kind, others_follow):
    """The power action to take after a job ("suspend" or "poweroff"), or "": only when it was chosen, everything went well (kind "success": nobody
    who sleeps through errors wants the computer off) and no other job runs or waits."""
    return after if after in ("suspend", "poweroff") and kind == "success" and not others_follow else ""
