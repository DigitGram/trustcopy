"""The TrustCopy entries of the right-click menu in Files (Nautilus), as a nautilus-python extension. desktop.py copies this
file to ~/.local/share/nautilus-python/extensions/ when the setting "extension" is on (it needs python3-nautilus).

It stands alone on purpose: every extension runs in one Python process inside Files, so it must not import the program's
modules (they have common names such as `core` and `i18n`). The texts, the name and the launcher come from menu.json, which
desktop.py writes and which is read each time the menu opens: a new name or language needs no restart of Files."""
import json
import os
import subprocess
from pathlib import Path

MENU_FILE = Path(os.environ.get("XDG_DATA_HOME") or "~/.local/share").expanduser() / "trustcopy" / "menu.json"
CLIPBOARD_FORMATS = ("x-special/gnome-copied-files", "text/uri-list")  # what "Paste from clipboard" reads


def entries_for(selected, background, clipboard_has_files):
    """The actions that fit: copy and move for a selection; paste for the folder background when the clipboard holds files."""
    if selected:
        return ["copy", "move"]
    return ["paste"] if background and clipboard_has_files else []


try:
    import gi
    gi.require_version("Gdk", "4.0")
    for _version in ("4.1", "4.0"):  # Files 50 ships the typelib as 4.1, older ones as 4.0
        try:
            gi.require_version("Nautilus", _version)
            break
        except ValueError:
            continue
    from gi.repository import Gdk, GObject, Nautilus
except (ImportError, ValueError):  # no Files here: the plain function above can still be used
    Nautilus = None

if Nautilus:
    class TrustCopyMenu(GObject.GObject, Nautilus.MenuProvider):
        def _config(self):
            try:
                return json.loads(MENU_FILE.read_text())
            except (OSError, ValueError):
                return None  # not written yet: no entries rather than wrong ones

        def _clipboard_has_files(self):
            try:
                formats = Gdk.Display.get_default().get_clipboard().get_formats()
                return any(formats.contain_mime_type(m) for m in CLIPBOARD_FORMATS)
            except Exception:  # noqa: BLE001 - cannot tell: show the entry, the program says what is wrong
                return True

        def _menu(self, selected, background, args):
            config = self._config()
            ops = entries_for(selected, background, self._clipboard_has_files()) if config else []
            if not ops:
                return []
            top = Nautilus.MenuItem(name="TrustCopy::menu", label=config["name"])
            sub = Nautilus.Menu()
            top.set_submenu(sub)
            for op in ops:
                item = Nautilus.MenuItem(name=f"TrustCopy::{op}", label=config["labels"][op])
                item.connect("activate", lambda _item, op=op: subprocess.Popen([config["launcher"], *args(op)], start_new_session=True))
                sub.append_item(item)
            return [top]

        def get_file_items(self, files):  # Files 43 and newer pass only the selection
            uris = [f.get_uri() for f in files]
            return self._menu(uris, False, lambda op: [op, "--", *uris])

        def get_background_items(self, current_folder):
            here = current_folder.get_uri()
            return self._menu([], True, lambda op: [op, "--to", here])
