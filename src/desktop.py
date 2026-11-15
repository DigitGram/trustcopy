# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DigitGram
"""The desktop side of TrustCopy, without GTK windows: the settings, the Files (Nautilus, Nemo, Caja) right-click
entries, the keyboard shortcuts and restarting the file managers. The copy engine is in core.py."""
import contextlib
import functools
import json
import os
import re
import shlex
import shutil
from pathlib import Path

try:
    from gi.repository import Gio, GLib  # only restarting Files needs them: everything else here runs without PyGObject
except ImportError:
    Gio = GLib = None

import i18n
from core import ALGOS
from i18n import N_, _

# --------------------------------------------------------------------------- settings

CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "trustcopy" / "settings.json"
CONFLICTS = ("ask", "skip", "keep_both", "newer", "overwrite")
AFTER = ("keep", "clean", "always")  # what the window does when the job is over
DEFAULTS = {"verify": True, "algo": "crc32", "conflict": "ask", "after": "keep",
            "notify": True, "sound": False, "menu": "scripts", "shortcuts": False, "keys": {}, "language": "auto"}


def shown(text):
    """Text for a label: file names need not be valid UTF-8, but GTK only takes text that is."""
    return os.fsencode(text).decode("utf-8", "replace")


def write_atomic(path, text):
    """Replace `path` with `text` all at once, so a crash never leaves half a file. A link stays a link
    and an existing file keeps its mode."""
    path = os.path.realpath(path)  # the rename would replace a link itself
    part = f"{path}.part"
    try:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(part)  # a stale part or a planted link: never write through it
        with open(part, "x", encoding="utf-8", errors="surrogateescape") as f:  # a name that is no UTF-8 goes in byte for byte
            f.write(text)
            f.flush()
            os.fsync(f.fileno())  # on the disk before it gets the name: a power cut must not leave an empty file (XFS does not flush on a rename)
        with contextlib.suppress(FileNotFoundError):  # a new file keeps the default mode; an old one its own
            shutil.copymode(path, part)
        os.replace(part, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(part)
        raise


def allowed():
    """Settings with a fixed set of values: {key: tuple of values}."""
    return {"algo": tuple(ALGOS), "conflict": CONFLICTS, "after": AFTER, "menu": ("scripts", "folder"),
            "language": ("auto", *i18n.languages())}


def load_config():
    """Saved settings merged over DEFAULTS, so a new setting never needs a migration. A value of the wrong
    type or outside allowed() counts as not saved, so a hand-edited file cannot crash a window.
    shortcut: the items of a list setting are not checked, the code that reads them filters."""
    config = dict(DEFAULTS)
    try:
        saved = json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):  # missing or corrupt file: use defaults
        return config
    if isinstance(saved, dict):
        choices = allowed()
        config.update({k: v for k, v in saved.items()
                       if k in DEFAULTS and type(v) is type(DEFAULTS[k]) and v in choices.get(k, (v,))})
    return config


def set_option(key, value):
    """Persist one setting. Only values that differ from DEFAULTS are written, so a better default later
    reaches everyone who never chose otherwise.

    The right-click entries and their shortcuts depend on the language, the menu style and the shortcut
    settings, so changing any of them rebuilds them; returns an error text (file not writable,
    install_scripts()'s), else None.
    """
    saved = {**load_config(), key: value}
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(CONFIG_FILE, json.dumps({k: v for k, v in saved.items() if v != DEFAULTS.get(k)}, indent=2))
    except OSError as e:
        return str(e)
    if key == "language":
        i18n.set_language(value)
    return install_scripts() if key in ("language", "menu", "shortcuts", "keys") else None


def reset_config():
    """Back to the defaults: forget every saved setting. Like set_option, returns an error text, else None."""
    try:
        if CONFIG_FILE.exists():  # emptied, not deleted: a link into a dotfiles folder stays a link
            write_atomic(CONFIG_FILE, "{}")
    except OSError as e:
        return str(e)
    i18n.set_language("auto")
    return install_scripts()  # the entries follow the language


# --------------------------------------------------------------------------- Files (Nautilus) integration
# shortcut: Nautilus scripts instead of a nautilus-python extension, so there is no extra dependency;
# the price is that the entries live under "Scripts", flat or in a "TrustCopy" submenu (setting "menu"), and that
# Files passes individually selected files in one environment variable (Linux caps it at 128 KiB: about 1,500 files of typical path
# length): beyond that the script silently does not start. A selected folder is not affected.
# Upgrade if users ask for a top-level entry.

DATA = Path(os.environ.get("XDG_DATA_HOME") or "~/.local/share").expanduser()
SCRIPTS_DIR = DATA / "nautilus/scripts"
# Nemo and Caja are forks of Files with its script mechanism: only the folder and the variables' prefix differ,
# so one text serves all three (it also keeps working when a user links their folders to one).
# Their shortcuts are left out: they save their key files themselves on quit and would overwrite ours.
FORKS = {"nemo": DATA / "nemo/scripts",
         "caja": Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "caja/scripts"}
SCRIPT_PREFIX = "TrustCopy: "  # keeps our entries together and recognisable in a crowded Scripts menu
MENU_DIR = "TrustCopy"  # the same entries as a submenu: Nautilus shows script folders as submenus
SCRIPT_ACTIONS = {"copy": N_("Copy to…"), "move": N_("Move to…"), "paste": N_("Paste from clipboard")}
# xargs keeps odd file names intact (no word splitting, no globbing)
_SELECTION = "printf '%s' \"{uris}\" | xargs -r -d '\\n' {bin} {op} --"
SCRIPT_BODIES = {
    "copy": _SELECTION,
    "move": _SELECTION,
    "paste": 'exec {bin} paste --to "{here}"',
}
_VARS = dict(uris="${NAUTILUS_SCRIPT_SELECTED_URIS:-${NEMO_SCRIPT_SELECTED_URIS:-$CAJA_SCRIPT_SELECTED_URIS}}",
             here="${NAUTILUS_SCRIPT_CURRENT_URI:-${NEMO_SCRIPT_CURRENT_URI:-$CAJA_SCRIPT_CURRENT_URI}}")


# Never change this text: older entries are recognised only by this line, so a new wording would orphan them all.
MARKER = "# Written by TrustCopy from its settings, replaced or deleted whenever they change"
LAUNCHER = Path("~/.local/bin/trustcopy").expanduser()  # where install.sh puts it: a run from a checkout must not bind the menu to itself


def script_text(op):
    """The content of the entry for `op`."""
    return "#!/bin/sh\n" + MARKER + "\n" + SCRIPT_BODIES[op].format(bin=shlex.quote(str(LAUNCHER)), op=op, **_VARS) + "\n"


def script_path(title, folder, base=None):
    """Where the Scripts entry for `title` lives (in `base`, Files' folder by default); its file name is the menu label."""
    base = base or SCRIPTS_DIR
    return base / MENU_DIR / _(title) if folder else base / (SCRIPT_PREFIX + _(title))


def menu_entries(base=None):
    """Paths of the right-click entries for the current language and menu style (what Files will list)."""
    folder = load_config()["menu"] == "folder"
    return [script_path(title, folder, base) for title in SCRIPT_ACTIONS.values()]


def files_setup():
    """What each file manager reads when it starts, by program: the entries it lists (the forks the same names in their
    own folders) and, for Files only, the shortcuts file. A change shows after a restart."""
    try:
        shortcuts = ACCELS_FILE.read_bytes()
    except OSError:
        shortcuts = b""
    entries = menu_entries()
    return {"nautilus": (entries, shortcuts), **dict.fromkeys(FORKS, entries)}


def _ours(path):
    """True for an entry we wrote (its second line is MARKER): only those are removed from the submenu folder."""
    try:
        if not path.is_file():  # a folder, or a FIFO, which would block open()
            return False
        with path.open() as f:
            f.readline(200)  # the shebang
            return f.readline(200).rstrip("\n") == MARKER
    except (OSError, ValueError):  # unreadable, or not text
        return False


def install_scripts(remove_only=False):
    """(Re)create the right-click entries in the current language and menu style, dropping stale ones
    (any language, any style, even from an older translation), for Files and for each fork that is installed.
    Returns an error text, or None on success."""
    try:
        seen = set()
        for fm, base in (("nautilus", SCRIPTS_DIR), *FORKS.items()):
            if base.resolve() in seen:  # linked to another one: a second pass would delete what the first wrote
                continue
            seen.add(base.resolve())
            folder = base / MENU_DIR
            for old in [*base.glob(SCRIPT_PREFIX + "*"), *(p for p in folder.glob("*") if _ours(p))]:
                old.unlink()
            with contextlib.suppress(OSError):  # not there, or holds files that are not ours
                folder.rmdir()
            if remove_only or not (fm == "nautilus" or shutil.which(fm)):
                continue  # a fork that is not installed gets no folder; its old entries are gone anyway
            for key, script in zip(SCRIPT_ACTIONS, menu_entries(base)):
                script.parent.mkdir(parents=True, exist_ok=True)
                script.write_text(script_text(key))
                script.chmod(0o755)
    except (OSError, RuntimeError) as e:  # RuntimeError: resolve() on a symlink loop
        return str(e)
    return install_accels(remove_only)


# --------------------------------------------------------------------------- shortcuts
# Files reads ~/.config/nautilus/scripts-accels when it starts: one "<accelerator> <script file name>" line per
# shortcut, and it skips "#" lines. Our lines sit between two such lines, so they are found again whatever
# language their names were written in. Files lets its own shortcuts win over a script's, and of two scripts
# on one key only one runs: hence the check before a key is written.
# shortcut: a key is checked against GNOME's shortcuts, the user's other script keys and a list of Files' own
# keys (version 46), so a global shortcut of another program, or a key a later Files adds, stays unseen.

ACCELS_FILE = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "nautilus/scripts-accels"
ACCEL_DEFAULTS = {"copy": "<Primary><Alt>c", "move": "<Primary><Alt>x", "paste": "<Primary><Alt>v"}  # next to Ctrl+C, X, V of Files
# Never change these two lines: older blocks are recognised by them.
ACCELS_BEGIN = "# TrustCopy shortcuts, written from its settings, replaced or deleted whenever they change"
ACCELS_END = "# end of TrustCopy shortcuts"
CUSTOM_SHORTCUT = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"  # Settings > Keyboard > custom shortcuts


def accel_key(accel):
    """A key in one spelling, so "<Control><Alt>X" and "<alt><Primary>x" compare equal; None if it is no key."""
    found = re.fullmatch(r"((?:<\w+>)+)(\w+)", accel.strip().lower())
    if not found:
        return None
    names = {"primary": "ctrl", "control": "ctrl", "mod1": "alt", "mod4": "super"}
    return frozenset(names.get(m, m) for m in re.findall(r"\w+", found[1])), found[2]


def accel_label(accel):
    """"<Primary><Alt>c" as "Ctrl+Alt+C"."""
    if not accel_key(accel):  # a bare key
        return accel
    mods, key = accel_key(accel)
    names = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "super": "Super"}
    return "+".join([*(names.get(m, m) for m in names if m in mods), key.title()])


# What Files binds itself (46.4; it wins over a script's key). Keys must carry Ctrl, Alt or Super, so only those are listed.
FILES_KEYS = {accel_key(k) for k in (
    *(f"<Primary>{c}" for c in "acdfhilmnoqrstvwxz0123"), *(f"<Primary><Shift>{c}" for c in "fimntz"), "<Primary><Alt>o",
    "<Primary>plus", "<Primary>minus", "<Primary>equal", "<Primary>Page_Up", "<Primary>Page_Down", "<Primary>Return",
    *(f"<Alt>{k}" for k in ("Left", "Right", "Up", "Down", "Home", "Return", *"012345678")))}


def gnome_shortcuts():
    """The keys GNOME itself uses, as accel_key()s: every setting of a "keybindings" schema, the media keys
    and the custom shortcuts. shortcut: found by schema name, so a shortcut kept elsewhere is missed."""
    taken = set()
    if Gio is None:
        return taken
    source = Gio.SettingsSchemaSource.get_default()
    for name in source.list_schemas(True)[0]:
        if "keybindings" not in name and not name.endswith(".media-keys"):
            continue
        settings = Gio.Settings.new(name)
        keys = settings.props.settings_schema.list_keys()
        for key in keys:
            value = settings.get_value(key).unpack()
            taken.update(accel_key(a) for a in (value if isinstance(value, list) else [value]) if isinstance(a, str))
        if "custom-keybindings" in keys and source.lookup(CUSTOM_SHORTCUT, True):
            for path in settings.get_strv("custom-keybindings"):
                if not (path.startswith("/") and path.endswith("/") and "//" not in path):
                    continue  # GSettings answers any other path with NULL, and PyGObject raises on that
                taken.add(accel_key(Gio.Settings.new_with_path(CUSTOM_SHORTCUT, path).get_string("binding")))
    taken.discard(None)
    return taken


def without_ours(text):
    """The shortcuts file without our block."""
    return re.sub(rf"^{re.escape(ACCELS_BEGIN)}\n.*?^{re.escape(ACCELS_END)}\n?", "", text, flags=re.S | re.M)


def shortcut_keys():
    """{action: key}: what the user chose (setting "keys") over the defaults; a value that is no key counts as not chosen."""
    chosen = load_config()["keys"]
    return {op: chosen[op] if isinstance(chosen.get(op), str) and accel_key(chosen[op]) else default for op, default in ACCEL_DEFAULTS.items()}


def taken_keys(foreign=None):
    """The keys of others, as accel_key()s: GNOME, Files and the other script lines (`foreign`: the shortcuts file
    without our block; read when not given)."""
    if foreign is None:
        try:
            foreign = without_ours(ACCELS_FILE.read_text(encoding="utf-8", errors="surrogateescape"))
        except OSError:
            foreign = ""
    lines = (line.partition(" ")[0] for line in foreign.splitlines() if not line.startswith("#"))
    return gnome_shortcuts() | FILES_KEYS | {accel_key(line) for line in lines}


def key_problem(accel, op, taken=None):
    """Why `accel` cannot be the key of action `op`: a short text, or None when it is fine."""
    key = accel_key(accel)
    if not key or not key[0] & {"ctrl", "alt", "super"}:  # a bare or shifted letter would break typing in Files
        return _("Use Ctrl, Alt or Super with the key.")
    mine = {accel_key(a) for other, a in shortcut_keys().items() if other != op}
    if key in mine | (taken_keys() if taken is None else taken):
        return _("Already in use.")
    return None


class Note(str):
    """A message that is no error, e.g. keys left out."""


def install_accels(remove_only=False):
    """Writes our shortcuts into the file Files reads, or takes them out (switch off, or remove_only). A key that is
    in use already is left out. Returns an error text, or a note naming the keys left out, else None."""
    try:
        old = ACCELS_FILE.read_text(encoding="utf-8", errors="surrogateescape")
    except FileNotFoundError:
        old = ""
    except OSError as e:
        return str(e)
    kept = without_ours(old)
    kept += "" if kept.endswith("\n") or not kept else "\n"
    lines, busy = [], []
    if load_config()["shortcuts"] and not remove_only:
        taken = taken_keys(kept)
        keys = shortcut_keys()
        for op, entry in zip(SCRIPT_ACTIONS, menu_entries()):
            if key_problem(keys[op], op, taken):
                busy.append(accel_label(keys[op]))
            else:
                lines.append(f"{keys[op]} {entry.name}")
    new = kept + ("\n".join([ACCELS_BEGIN, *lines, ACCELS_END]) + "\n" if lines else "")
    try:
        if new != old and new.strip():
            ACCELS_FILE.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(ACCELS_FILE, new)
        elif new != old:
            write_atomic(ACCELS_FILE, new) if ACCELS_FILE.is_symlink() else ACCELS_FILE.unlink()  # nothing but our block was in it; a dotfiles link stays a link
    except OSError as e:
        return str(e)
    return Note(_("Already in use, so not set: {keys}").format(keys=", ".join(busy))) if busy else None


# --------------------------------------------------------------------------- restarting the file managers

FILE_MANAGERS = {"nautilus": "org.gnome.Nautilus", "nemo": "org.Nemo", "caja": "org.mate.Caja"}  # program: its bus name
# shortcut: nemo-desktop (its own process) is not restarted, so the desktop's right-click menu changes only at the next login;
# and in MATE the session restarts Caja too, so the one we start may win and run outside the session till then. Upgrade if users report it.
waits = []  # the waits for a file manager to quit: at most one per program, whichever window asked for it


def files_running(programs):
    """Those of `programs` that own their bus name, so a restart has something to restart.
    If the bus cannot say, ask for Files anyway, as before; a fork only when the bus says it runs."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return [fm for fm in programs if bus.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
            GLib.Variant("(s)", (FILE_MANAGERS[fm],)), GLib.VariantType("(b)"), Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]]
    except GLib.Error:
        return [fm for fm in programs if fm == "nautilus"]


def restart_files(app, toast, programs):
    """Quits each of `programs` and opens it again once it has left the bus (a fixed pause races with it). The app is held
    meanwhile: closing the last window now must not end the program before they are back."""
    for fm in programs:
        if any(watch[2] == fm for watch in waits):  # a restart is under way already
            continue
        try:
            Gio.Subprocess.new([fm, "-q"], Gio.SubprocessFlags.NONE)
        except GLib.Error as e:
            toast(e.message)
            continue
        app.hold()
        watch = (Gio.bus_watch_name(Gio.BusType.SESSION, FILE_MANAGERS[fm], Gio.BusNameWatcherFlags.NONE, None,
                                    functools.partial(files_gone, toast, fm)), app, fm)
        waits.append(watch)
        GLib.timeout_add_seconds(30, stop_waiting, watch)  # a file manager that refuses to quit stays as it is


def stop_waiting(watch):
    """Ends the wait for a file manager, unless it is over already."""
    if watch in waits:
        waits.remove(watch)
        Gio.bus_unwatch_name(watch[0])
        watch[1].release()
    return GLib.SOURCE_REMOVE


def files_gone(toast, fm, connection, name):
    """`fm` has left the bus: open it again. The bus watch passes `connection` and `name`; neither is needed."""
    watch = next((w for w in waits if w[2] == fm), None)
    if not watch:  # we gave up waiting already
        return
    stop_waiting(watch)
    try:
        # the program itself, not the default folder handler: that may be another one, and Caja's starts with --no-desktop
        Gio.Subprocess.new([fm, os.path.expanduser("~")], Gio.SubprocessFlags.NONE)
    except GLib.Error as e:
        toast(e.message)
