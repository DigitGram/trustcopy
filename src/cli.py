# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DigitGram
"""Terminal mode: copy and move answered as text. No GTK is imported here, and the command line is defined here so
that both front ends share it.

trustcopy.py picks this module for `copy` and `move` when stdout is a terminal and a destination is given (--to);
without one the window asks for the folder, as before, and `paste` needs the clipboard, so it stays a window. The look
follows install.sh: a banner, then one line per step with [✓] [✗] [!] [i] in front, no colours. Exit codes: 0 done,
1 a file failed or the job was cancelled or stopped, 2 the command could not run.

shortcut: the labels of the tables (source, target, MISMATCH) are English in every language, as in cp and rsync; the
sentences the window says too come from the translations. Upgrade if somebody asks for more.
"""
import argparse
import os
import shutil
import sys
import threading
import time
from datetime import datetime
from urllib.parse import unquote, urlparse

import core
import desktop
from i18n import _, _n

OK, FAILED, USAGE = 0, 1, 2
TICK, CROSS, WARN, INFO = "[✓]", "[✗]", "[!]", "[i]"
BANNER = r"""
    ______                __  ______
   /_  __/______  _______/ /_/ ____/___  ____  __  __
    / / / ___/ / / / ___/ __/ /   / __ \/ __ \/ / / /
   / / / /  / /_/ (__  ) /_/ /___/ /_/ / /_/ / /_/ /
  /_/ /_/   \__,_/____/\__/\____/\____/ .___/\__, /
                                     /_/    /____/
"""
TAGLINE = "Verified copying for Nautilus  ·  GPL-3.0-or-later"
ALGO_NAMES = {"crc32": "CRC32", "blake2b": "BLAKE2b", "sha256": "SHA-256"}
POLICIES = ("ask", "skip", "keep_both", "newer", "overwrite")

parser = argparse.ArgumentParser(prog="trustcopy", usage="%(prog)s copy|move --to DESTINATION SOURCE... [options]\n       %(prog)s paste [--to DESTINATION] | settings | integrate [--remove]")
parser.add_argument("op", nargs="?", choices=["copy", "move", "paste", "settings"])
parser.add_argument("--to", metavar="FOLDER", help="the destination folder, where the SOURCEs are put (copy/move asks if omitted; paste defaults to the current folder)")
parser.add_argument("--gui", action="store_true", help="open the window even when started from a terminal")
parser.add_argument("-q", "--quiet", action="store_true", help="in a terminal: no banner and only the files that are not fine")
parser.add_argument("--on-conflict", choices=POLICIES, help="in a terminal: what to do with a file that exists (default: the setting; ask needs a terminal)")
parser.add_argument("--no-verify", action="store_true", help="in a terminal: copy without the checksum check")
parser.add_argument("files", nargs="*", metavar="SOURCE", help="the files and folders to copy or move")


# --------------------------------------------------------------------------- small helpers

def _width():
    return max(40, min(shutil.get_terminal_size((80, 24)).columns, 100))


def _line(text="", marker=None, indent=2):
    print(" " * indent + (f"{marker} " if marker else "") + text)


def _rule():
    print("  " + "─" * (_width() - 4))


def _cut(text, n):
    """`text` in at most n characters: the ends stay, the middle goes (a name's extension matters)."""
    if len(text) <= n:
        return text
    keep = n - 1
    return text[:keep - keep // 3] + "…" + text[len(text) - keep // 3:]


def _duration(seconds):
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min {seconds} s"


def _size(n):
    """Like Files shows it: units of 1000, one decimal from kB up."""
    for unit in ("B", "kB", "MB", "GB"):
        if round(n, 1) < 1000:
            break
        n /= 1000
    else:
        unit = "TB"
    return f"{n:.{0 if unit == 'B' else 1}f} {unit}"


def _rate(nbytes, clock):
    """Bytes/s as text over the I/O seconds in `clock`, "" when too little was moved to tell (as the window does)."""
    return f"{_size(nbytes / clock.secs)}/s" if nbytes >= 1 << 20 and clock.secs > 0.001 else ""


def _stamp(path):
    st = os.lstat(path)  # lstat: a dangling link must not break the prompt
    return f"{_size(st.st_size)} · {datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M}"


def _local(arg):
    """A plain path or a file:// URI (Files sends those) as an absolute local path; None for anything else."""
    if "://" in arg:
        uri = urlparse(arg)
        return os.path.abspath(unquote(uri.path)) if uri.scheme == "file" else None
    return os.path.abspath(arg)


def _fail(text):
    print(f"trustcopy: {text}", file=sys.stderr)
    return USAGE


# --------------------------------------------------------------------------- the running job

class Screen:
    """The bar on stderr, redrawn in place, and the one place that may write while it runs: the conflict prompt
    takes the screen for its question, the bar waits. Nothing is drawn when stderr is no terminal (a log stays clean)."""

    def __init__(self, job, move):
        self.job, self.on, self.move = job, sys.stderr.isatty(), move
        self.lock = threading.Lock()
        self.start = time.monotonic()

    def draw(self):
        job = self.job
        if not self.on or not job.total:
            return
        frac = job.step_fraction
        fill = int(frac * 24)
        bar = "=" * fill + (">" if 0 < fill < 24 else "")
        step = _("Verifying") if job.phase == "verify" else (_("Moving") if self.move else _("Copying"))
        speed = job.moved / max(time.monotonic() - self.start, 0.001)
        name = desktop.shown(os.path.basename(job.current)) if job.current else ""
        text = f"  [{bar:<24}] {int(frac * 100):3d}%  {_size(speed) + '/s':>10}  {step}  {name}"
        with self.lock:
            sys.stderr.write("\r\033[K" + _cut(text, _width() - 1))
            sys.stderr.flush()

    def clear(self):
        if self.on:
            with self.lock:
                sys.stderr.write("\r\033[K")
                sys.stderr.flush()

    def ask(self, src, dst):
        """The job's conflict callback, in a worker thread. A terminal is asked; without one nobody can answer: skip."""
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return None
        keys = {"k": "keep_both", "s": "skip", "n": "newer", "o": "overwrite", "c": "cancel"}
        with self.lock:  # the bar stays away until the answer is in
            if self.on:
                sys.stderr.write("\r\033[K")
            print()
            new, old, exists, every = _("New"), _("Existing"), _("File already exists"), _("Apply to all remaining conflicts")
            both, skip, newer, over, cancel = _("Keep both"), _("Skip"), _("Overwrite if newer"), _("Overwrite"), _("Cancel")
            _line(exists + f": {desktop.shown(os.path.basename(dst))}", WARN)
            _line(f"{new:<10}{_stamp(src)}", indent=6)
            _line(f"{old:<10}{_stamp(dst)}", indent=6)
            print()
            _line(f"[k] {both}   [s] {skip}   [n] {newer}   [o] {over}   [c] {cancel}", indent=6)
            _line(f"a capital letter: {every}", indent=6)
            while True:
                try:
                    answer = input("      > ").strip()
                except (EOFError, KeyboardInterrupt):
                    return "cancel", False
                if answer[:1].lower() in keys and len(answer) == 1:
                    print()
                    return keys[answer.lower()], answer.isupper()


def _outcome(job, ok, bad):
    """(title, text, marker) of the result line: the same words as the window's (ui_common.outcome)."""
    if job.fatal:
        return _("Stopped"), desktop.shown(job.fatal), CROSS
    if job.cancelled:
        return _("Cancelled"), _n("{n} file was done before cancelling.", "{n} files were done before cancelling.", ok).format(n=ok), WARN
    if bad:
        return _("Finished with errors"), _n("{ok} file done, {bad} failed.", "{ok} files done, {bad} failed.", ok).format(ok=ok, bad=len(bad)), CROSS
    if not ok and job.results:
        n = len(job.results)
        return _("Skipped"), _n("{n} file skipped", "{n} files skipped", n).format(n=n), WARN
    checked = bool(ok) and all(r.status == "verified" for r in job.results if r.status in ("ok", "verified"))
    if job.move:
        text = (_n("{n} file moved and verified successfully", "{n} files moved and verified successfully", ok) if checked
                else _n("{n} file moved successfully", "{n} files moved successfully", ok))
    else:
        text = (_n("{n} file copied and verified successfully", "{n} files copied and verified successfully", ok) if checked
                else _n("{n} file copied successfully", "{n} files copied successfully", ok))
    text = text.format(n=ok)
    if skipped := sum(r.status == "skipped" for r in job.results):
        return _("Done"), text + " · " + _n("{n} file skipped", "{n} files skipped", skipped).format(n=skipped), WARN
    return _("Done"), text, TICK


# --------------------------------------------------------------------------- the command

def run(args):
    """`copy` or `move` with a destination, answered in the terminal. Returns the exit code."""
    move = args.op == "move"
    sources = [_local(f) for f in args.files if f]
    dest = _local(args.to)
    if not sources:
        return _fail(_("No files to work on."))
    if None in sources or dest is None:
        return _fail(_("Some selected items are not files on this computer. Nothing was started."))
    if os.path.exists(dest) and not os.path.isdir(dest):
        return _fail(f"{dest}: " + _("The destination is not a folder on this computer."))
    cfg = desktop.load_config()
    verify, policy = cfg["verify"] and not args.no_verify, args.on_conflict or cfg["conflict"]
    items = core.items_for(sources, dest)
    try:
        job = core.Job(items, move=move, verify=verify, policy=policy, algo=cfg["algo"])
    except (ValueError, OSError) as exc:
        return _fail(str(exc))
    screen = Screen(job, move)
    job.ask = screen.ask

    if not args.quiet:
        print(BANNER + f"  {TAGLINE}\n")
    where = os.path.commonpath([os.path.dirname(src) for src, _dst in items])
    verb = _("Moving") if move else _("Copying")
    _line(f"{verb}  ·  {len(items)} {'item' if len(items) == 1 else 'items'}  ·  "
          f"{ALGO_NAMES[cfg['algo']] + ' check' if verify else 'no check'}", INFO)
    _line(f"from  {desktop.shown(where)}", indent=6)
    _line(f"to    {desktop.shown(dest)}", indent=6)
    print()

    thread = threading.Thread(target=job.run, daemon=True)
    thread.start()
    while thread.is_alive():
        try:
            screen.draw()
            thread.join(0.15)
        except KeyboardInterrupt:  # one Ctrl+C stops cleanly: temp files go, finished files stay
            job.cancel()
    screen.clear()
    return _report(job, dest, args.quiet, time.monotonic() - screen.start)


def _report(job, dest, quiet, seconds):
    results = sorted(job.results, key=lambda r: r.dst.casefold())
    bad = [r for r in results if r.status == "failed"]
    ok = sum(r.status in ("ok", "verified") for r in results)
    names = [desktop.shown(os.path.relpath(r.dst, dest)) for r in results]
    width = min(max(map(len, names), default=0), 44)
    words = {"verified": _("Verified"), "ok": _("Done"), "skipped": _("Skipped"), "failed": _("Failed")}
    for r, name in zip(results, names):
        if quiet and r.status in ("verified", "ok"):
            continue
        marker = {"verified": TICK, "ok": TICK, "skipped": WARN, "failed": CROSS}.get(r.status, INFO)
        size = _size(r.size) if r.status in ("verified", "ok") else ""
        _line(f"{_cut(name, width).ljust(width)}  {words.get(r.status, r.status):<9} {size:>9}", marker)
        if r.status == "failed":
            _line(desktop.shown(r.error), indent=8)
            if r.source and r.target:
                _line(f"source  {r.source}", indent=8)
                _line(f"target  {r.target}", indent=8)
    if results:
        print()
    title, text, marker = _outcome(job, ok, bad)
    _rule()
    _line(f"{title}  ·  {text}", marker)
    done = sum(r.size for r in results if r.status in ("ok", "verified"))
    figures = [f"{_size(done)} in {_duration(seconds)}"]
    if speed := _rate(job.written, job.writing):
        figures.append(f"write {speed}")
    if speed := _rate(job.readback, job.reading):
        figures.append(f"read {speed}")
    if any(r.digest for r in results):
        figures.append(f"{ALGO_NAMES[job.algo]} checksum")
    _line("  ·  ".join(figures), INFO)
    print()
    return FAILED if bad or job.fatal or job.cancelled else OK
