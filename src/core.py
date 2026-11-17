# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DigitGram
"""The TrustCopy copy engine without GTK: verification, conflict handling, pause and clean cancel, so it can be
tested headless. Settings and the Files (Nautilus) integration live in desktop.py."""
import contextlib
import ctypes
import errno
import fcntl
import hashlib
import os
import re
import shutil
import stat
import threading
import time
import unicodedata
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

try:
    from gi.repository import GLib  # only for a readable size in one message: the engine runs without PyGObject
except ImportError:
    GLib = None

from i18n import _

__version__ = "1.1.1"
AUTHOR = "DigitGram"
COPYRIGHT = "© 2026 DigitGram"

CHUNK = 1 << 20  # small enough for a smooth progress bar even on slow network shares
SYNC_MIN, SYNC_MAX = 1 << 20, 32 << 20  # flush after this much: about 0.2 s of data, within these limits. The page cache swallows the first gigabytes, the graph and the ETA would show RAM speed
RETRIES = 3
PART = ".trustpart"  # shortcut: names within 10 bytes of the 255 limit fail (reported as an error), fix if it ever bites
FICLONE = 0x40049409
SPARSE_MIN = 1 << 20  # a file is looked at for holes from this size, and when at least this much of it is one
FALLOC_FL_KEEP_SIZE = 1  # claim the clusters only: mode 0 makes FAT write the whole file full of zeros first
FULL = {errno.ENOSPC, errno.EDQUOT}  # the disk or the user's quota: every following file would fail too
NO_SYNC = {errno.EINVAL, errno.ENOTSUP, errno.ENOSYS}  # a file system that cannot flush
NO_KERNEL_COPY = {errno.EXDEV, errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}

try:  # the raw call, not os.posix_fallocate: glibc emulates it by writing every block when the filesystem has none (SMB, FUSE)
    _fallocate = ctypes.CDLL(None, use_errno=True).fallocate64
    _fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int64, ctypes.c_int64]
except (OSError, AttributeError):
    _fallocate = None


def _reserve(fd, size, offset=0):
    """Claim the room for a file before writing it: a full target stops a 40 GB file at once, not after 40 GB.
    True if reserved. Only a full target and a file too big for the filesystem (over 4 GiB on FAT32, which would fail after 4 GiB,
    three times) are errors: where the filesystem cannot reserve (exFAT and many network shares refuse KEEP_SIZE), copying goes on as before.
    shortcut: a compressing filesystem (btrfs) reserves the uncompressed size, so a nearly full one may refuse a file that would have fit."""
    if not (size and _fallocate):
        return False
    if _fallocate(fd, FALLOC_FL_KEEP_SIZE, offset, size) == 0:
        return True
    if (code := ctypes.get_errno()) in FULL | {errno.EFBIG}:
        raise OSError(code, os.strerror(code))
    return False


def _data_extents(fd):
    """[(offset, length)] of the parts of an open file that hold data, and its size, when it has holes of 1 MiB or more (a sparse file: a VM disk,
    a preallocated download); None for an ordinary file and wherever the filesystem cannot say (SEEK_DATA/SEEK_HOLE)."""
    try:
        st = os.fstat(fd)
        if not (stat.S_ISREG(st.st_mode) and st.st_size >= SPARSE_MIN and st.st_blocks * 512 + SPARSE_MIN <= st.st_size):
            return None  # what the filesystem has allocated is the file, or all but a little of it
        found, pos = [], 0
        while pos < st.st_size:
            try:
                start = os.lseek(fd, pos, os.SEEK_DATA)
            except OSError as e:
                if e.errno == errno.ENXIO:  # no data after pos: the rest is a hole
                    break
                raise
            end = os.lseek(fd, start, os.SEEK_HOLE)
            found.append((start, end - start))
            pos = end
        os.lseek(fd, 0, os.SEEK_SET)
        return found, st.st_size
    except (OSError, AttributeError):
        return None


def _room(src, size):
    """The room a copy of the file takes on the target: its size, or for a sparse file (holes of 1 MiB or more, which stay holes) what it has allocated."""
    try:
        allocated = os.lstat(src).st_blocks * 512
    except OSError:
        return size
    return allocated if size >= SPARSE_MIN and allocated + SPARSE_MIN <= size else size


def _sync_dir(path):
    """Put a folder's entries on the disk: a rename lives in the cache until then, and a crash or a pulled stick undoes it."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    except OSError as e:
        if e.errno not in NO_SYNC:  # some FUSE/SMB mounts cannot flush a folder; there is nothing better to do
            raise
    finally:
        os.close(fd)


TIME_FUZZ_NS = 2_000_000_000  # FAT keeps the time to two seconds: a copy of a copy there is not "older"


def compare(src, dst):
    """How the new file compares with the existing one, for the conflict question: (text, kind) with kind "same" (a hint that it is the very file, never
    a proof), "good" (only things in favour of the new one) or "bad" (something speaks against it). Cheap: size and modification time, no reading."""
    new, old = os.lstat(src), os.lstat(dst)
    parts = []  # (word, is it in favour of the new file)
    if abs(new.st_mtime_ns - old.st_mtime_ns) >= TIME_FUZZ_NS:
        parts.append((_("Newer"), True) if new.st_mtime_ns > old.st_mtime_ns else (_("Older"), False))
    if new.st_size != old.st_size:
        parts.append((_("Larger"), True) if new.st_size > old.st_size else (_("Smaller"), False))
    if not parts:
        return _("Same size and time"), "same"
    # TRANSLATORS: the separator between two words of the hint on a conflict, as in "Newer, larger"
    text = _(", ").join([parts[0][0]] + [word.lower() for word, _good in parts[1:]])
    return text, "good" if all(good for _word, good in parts) else "bad"


def _stamp(st):
    """What tells a source apart from the one that was read: saving a file changes its times (ctime even when a tool sets mtime back), mostly its size too.
    shortcut: a save of the same size within the time resolution of the source's filesystem (2 s on FAT) is not seen; hash the source again if that bites."""
    return st.st_size, st.st_mtime_ns, st.st_ctime_ns


class Cancelled(Exception):
    pass


class _Crc32:
    """hashlib-style wrapper so every checksum is used the same way."""

    def __init__(self):
        self.value = 0

    def update(self, data):
        self.value = zlib.crc32(data, self.value)

    def digest(self):
        return self.value.to_bytes(4, "big")


# crc32: fastest, catches accidental corruption; blake2b/sha256: cryptographic
ALGOS = {"crc32": _Crc32, "blake2b": hashlib.blake2b, "sha256": hashlib.sha256}


class _Tee:
    """Feeds the bytes of a copy to the checksum that verifies it (`main`, may be None) and to the one for the checksum file (`side`)."""

    def __init__(self, main, side):
        self.main, self.side = main, side

    def update(self, data):
        if self.main:
            self.main.update(data)
        self.side.update(data)


# the checksum file: key -> (hasher, name for several items, ending for one, digest length in hex)
SUM_ALGOS = {
    "sha256": (hashlib.sha256, "SHA256SUMS", ".sha256", 64),
    "sha512": (hashlib.sha512, "SHA512SUMS", ".sha512", 128),
    "md5": (lambda: hashlib.md5(usedforsecurity=False), "MD5SUMS", ".md5", 32),  # not for security: it is the format old mirrors and tools read
    "crc32": (_Crc32, "checksums.sfv", ".sfv", 8),
}


def format_sums(algo, entries):
    """The text of a checksum file from {path: hex}: GNU style (`hash  path`, what sha256sum -c reads), or SFV (`path CRC`) for crc32.
    A name with a backslash or a line break is escaped the GNU way."""
    lines = []
    for path, digest in entries.items():
        if algo == "crc32":
            lines.append(f"{path} {digest.upper()}")
        elif "\\" in path or "\n" in path:
            lines.append("\\" + digest + "  " + path.replace("\\", "\\\\").replace("\n", "\\n"))
        else:
            lines.append(f"{digest}  {path}")
    return "".join(line + "\n" for line in lines)


def parse_sums(algo, text):
    """{path: hex} of a checksum file. Comments (# or ;), empty lines and anything that is no entry are left out."""
    entries, size = {}, SUM_ALGOS[algo][3]
    for line in text.splitlines():
        if algo == "crc32":
            m = re.match(r"^(.*\S)\s+([0-9A-Fa-f]{8})\s*$", line) if not line.startswith(";") else None
            if m:
                entries[m[1]] = m[2].lower()
            continue
        m = re.match(r"^(\\?)([0-9A-Fa-f]{%d}) [ *](.+)$" % size, line)
        if m:
            path = re.sub(r"\\(.)", lambda e: "\n" if e[1] == "n" else e[1], m[3]) if m[1] else m[3]
            entries[path] = m[2].lower()
    return entries


@dataclass
class Result:
    src: str
    dst: str
    status: str  # ok | copied (waits for its check, only while the job runs) | verified | skipped | failed
    error: str = ""
    size: int = 0
    digest: str = ""  # hex checksum of the verified copy
    kept: bool = False  # a bad copy the late check left in place: a retry replaces it without asking
    source: str = ""  # hex checksum of the source as it was read and of the copy as it was read back: the same for a good copy, different for a bad one
    target: str = ""


def items_for(sources, dest):
    """Pair every source with its target path inside the destination folder."""
    return [(s, os.path.join(dest, os.path.basename(os.path.abspath(s)))) for s in sources]


def parse_clipboard(text):
    """Parse Files' x-special/gnome-copied-files (or a plain URI list) into (op, uris)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    op = "copy"
    if lines and lines[0] in ("copy", "cut"):
        op = "move" if lines.pop(0) == "cut" else "copy"
    return op, lines


def name_key(path):
    """A target path the way lenient filesystems compare names: case (exFAT, NTFS, SMB), trailing dots (FAT, exFAT), NFC/NFD (macOS, ZFS).
    shortcut: an approximation of those rules, and on a strict target it only costs a "(2)"; upgrade if one differs."""
    return re.sub(r"\.+(?=/|$)", "", unicodedata.normalize("NFC", path)).casefold()


def free_name(path, claimed=()):
    """`path` with " (2)", " (3)", … before the extension: the first name that is neither on disk nor in `claimed` (as name_key)."""
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.lexists(f"{stem} ({n}){ext}") or name_key(f"{stem} ({n}){ext}") in claimed:
        n += 1
    return f"{stem} ({n}){ext}"


def find_leftovers(folders, deep=False):
    """The unfinished copies (`name.trustpart`) a hard stop left in `folders`, as sorted (path, size). `deep`: the folders below them too.
    Links are never followed, and a folder is never a leftover. Only call it while no job writes there: a running job's temp files look the same."""
    found, todo = [], list(folders)
    while todo:
        try:
            with os.scandir(todo.pop()) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        if deep:
                            todo.append(entry.path)
                    elif name_key(entry.name).endswith(PART):
                        found.append((entry.path, entry.stat(follow_symlinks=False).st_size))
        except OSError:
            continue  # a folder that cannot be read has nothing to show
    return sorted(set(found))


def remove_leftovers(paths):
    """Deletes what find_leftovers found, after looking at each again. Returns (how many are gone, [(path, error)])."""
    gone, errors = 0, []
    for path in paths:
        try:
            if stat.S_ISDIR(os.lstat(path).st_mode) or not name_key(os.path.basename(path)).endswith(PART):
                raise OSError(errno.EINVAL, _("Not an unfinished copy"))  # changed since it was listed: never delete anything else
            os.unlink(path)
            gone += 1
        except FileNotFoundError:
            gone += 1  # gone already, which is what was wanted
        except OSError as e:
            errors.append((path, e.strerror or str(e)))
    return gone, errors


def _rotational(path):
    """True if path lives on a spinning disk, where parallel copies only add seeks."""
    try:
        dev = os.stat(path).st_dev
        node = Path(f"/sys/dev/block/{os.major(dev)}:{os.minor(dev)}").resolve()
        for d in (node, node.parent):  # partition -> whole disk
            flag = d / "queue" / "rotational"
            if flag.exists():
                return flag.read_text().strip() == "1"
    except OSError:
        pass
    return False  # network, FUSE, btrfs: unknown, parallel is fine


class Clock:
    """Wall-clock seconds during which at least one thread was inside `with clock:`, so parallel workers count once."""

    def __init__(self):
        self.secs, self._n, self._t, self._lock = 0.0, 0, 0.0, threading.Lock()

    def __enter__(self):
        with self._lock:
            if not self._n:
                self._t = time.monotonic()
            self._n += 1

    def __exit__(self, *exc):
        with self._lock:
            self._n -= 1
            if not self._n:
                self.secs += time.monotonic() - self._t


class Job:
    """One copy or move. run() blocks, so call it in a thread and poll the
    public fields (done, total, phase, current, results, failed, finished, fatal)."""

    def __init__(self, items, move=False, verify=True, policy="ask", ask=None, workers=None, algo="crc32", replace=(), dirs=(), sums=None):
        """policy: ask | skip | keep_both | newer | overwrite. algo: a key of ALGOS. replace: targets that are bad copies of an earlier run.
        dirs: source folders of that earlier run, which a move empties and removes.
        ask(src, dst) -> (policy, remember) | None; it runs in a worker thread and may block."""
        self.move, self.verify, self.policy, self.ask = move, verify, policy, ask
        self.algo, self.replace, self.dirs = algo, set(replace), list(dirs)
        self.sums = sums  # None, or a key of SUM_ALGOS: also hash every file for the checksum file; the window can switch it on while the job runs
        self._sums, self.sum_read = {}, 0  # temp path -> hex checksum of the file as it was written; bytes read later for files that have none
        self._hasher = ALGOS[algo]
        self.items = self._checked_items(items)
        self.workers = workers or (1 if any(_rotational(s) or _rotational(os.path.dirname(d))
                                            for s, d in self.items) else 4)
        self.done = self.count = self.size = 0
        self.written, self.readback = 0, 0  # bytes written / read back for verification ...
        self.writing, self.reading = Clock(), Clock()  # ... and the time each took: write and read speed
        self._later = 0  # units of the started files that wait for step 2: they are not part of step 1
        self.check_base = 0  # `done` when step 2 began: its progress starts from there
        self._unstarted = self._committed = 0  # progress units: bytes still to start / already promised to started files
        self.late, self.check_pass = [], False  # results changed by phase 2 (the checks); check_pass: that phase is running
        self._skipped = 0  # units of read-backs dropped because verification was switched off: counted done, but no byte moved for them
        self._to_check, self._checks, self._deferred = [], 0, {}  # copies waiting in their temp files for phase 2: (result, source digest, temp path); their bytes; temp path -> digest (not the source: one source can be in a job twice)
        self._stamps = {}  # a move's temp path -> _stamp() of its source when it was opened for reading
        self.stage, self.current, self.fatal = "scan", "", ""
        self.phases = {}  # the files in flight: source -> "copy" or "verify". Workers run side by side, so a phase is the file's, not the job's
        self.results, self.files, self.finished = [], [], False  # files: the planned (src, dst, kind, size)
        self._run = threading.Event()
        self._run.set()
        self._cancel = threading.Event()
        self._progress_lock, self._ask_lock, self._claim_lock, self._release_lock = threading.Lock(), threading.Lock(), threading.Lock(), threading.Lock()
        # targets promised to a file of this job: two sources with one name must never meet there.
        # shortcut: kept as name_key(), as exFAT/NTFS/SMB/FAT targets treat "Readme" and "README" (or "x." and "x") as one file; name_key only approximates their rules, upgrade if one differs
        self._claimed = set()
        self._sources = set()  # name_key() of the files this job is yet to read: overwriting one would destroy it before its turn

    def _checked_items(self, items):
        """The (src, dst) pairs as absolute paths. A move onto itself is left out (and listed as skipped), a copy onto itself gets a free name,
        and a folder into itself is refused."""
        checked, self._there = [], []
        for src, dst in items:
            src, dst = os.path.abspath(src), os.path.abspath(dst)
            if os.path.lexists(src) and os.path.lexists(dst) and os.path.samestat(os.lstat(src), os.lstat(dst)):
                if self.move:
                    self._there.append(Result(src, dst, "skipped"))  # already there: nothing to do, and "0 files moved" would not say so
                    continue
                dst = free_name(dst)  # copied onto itself
            real = os.path.realpath(src)
            if not os.path.islink(src) and os.path.isdir(real) and os.path.commonpath([real, os.path.realpath(dst)]) == real:
                raise ValueError(_("Cannot put a folder into itself: {path}").format(path=src))
            checked.append((src, dst))
        return checked

    # --- control -----------------------------------------------------------
    def pause(self):
        self._run.clear()

    def resume(self):
        self._run.set()

    def cancel(self):
        self._cancel.set()
        self._run.set()  # wake paused workers so they can stop

    # every byte is read again when verifying; `verify` may be switched while running, so the total follows it
    passes = property(lambda self: 2 if self.verify else 1)
    # the read-backs of finished copies are promised in `_committed`; switching verification off takes them out of the total at once, so the bar does not jump at the end
    total = property(lambda self: self._committed + self._unstarted * self.passes - (0 if self.verify else self._checks))
    # step 1 ends where the read-backs begin: their units (the waiting copies, and the second pass of files not started) are not part of it
    first_pass = property(lambda self: self.total - ((self._later + self._unstarted * (self.passes - 1)) if self.verify else 0))
    @property
    def step_span(self):
        """`done` at the start and at the end of the step under way: writing, then the read-backs."""
        return (self.check_base, self.total) if self.check_pass else (0, self.first_pass)
    @property
    def _step(self):
        """(done, size) of the step under way, in units."""
        base, end = self.step_span
        done, size = self.done - base, end - base
        if not self.check_pass:  # step 1 is the writing: the read-back that switching verification off drops is no part of it
            done, size = done - self._skipped, size - self._skipped - self._dropping
        return done, size
    @property
    def step_fraction(self):
        """How far the step under way is, 0 to 1: writing, then the read-backs, each from the start."""
        done, size = self._step
        return min(max(done, 0) / size, 1) if size > 0 else 0
    @property
    def step_left(self):
        """Bytes the step under way still has to move: the time remaining follows the bar, which fills per step."""
        done, size = self._step
        return max(size - done, 0)
    # the read-backs that files in flight were promised while verification was on and that switching it off will skip: until a file
    # is written they still sit in the total, and would halve the bar
    _dropping = property(lambda self: 0 if self.verify else self._later - self._checks)
    moved = property(lambda self: self.done - self._skipped)  # what really moved, for the speed
    @property
    def phase(self):
        """What the heading says: while files are in flight what they do (writing first), else the stage of the job."""
        now = tuple(self.phases.values())
        return "copy" if "copy" in now else "verify" if now else self.stage
    paused = property(lambda self: not self._run.is_set())
    cancelled = property(lambda self: self._cancel.is_set())
    failed = property(lambda self: sum(r.status == "failed" for r in self.results))

    # --- running -----------------------------------------------------------
    def run(self):
        made = []
        plan = []
        try:
            plan = self._scan()
            plan = self._make_dirs(plan, made)
            if self.move:  # a new folder's name is an entry of its parent (FAT keeps it in the cache): on the disk before any source goes
                for parent in {os.path.dirname(dst) for _src, dst, _times in made}:
                    _sync_dir(parent)
            self.dirs += [src for src, _dst, kind, _size in plan if kind == "dir"]
            files = sorted((p for p in plan if p[2] != "dir"), key=lambda p: (os.path.basename(p[0]).casefold(), p[0]))  # A-Z, as listed
            self.files = files
            self._sources = {name_key(p[0]) for p in files}
            self.count = len(files)
            self.size = sum(size for _, _, kind, size in files if kind == "file")  # bytes of data to copy
            self._unstarted = self.size
            self.fatal = self._missing_room(files)  # nothing is written to a target that cannot hold it
            if self.fatal:
                raise Cancelled
            self.stage = "copy"
            with ThreadPoolExecutor(self.workers) as pool:
                list(pool.map(lambda p: self._run_item(*p), files))
            if not self.cancelled:
                self._run_checks()
        except Cancelled:
            pass
        except Exception as e:
            self.fatal = self.fatal or str(e)
        finally:
            self._drop_unchecked()
            self._finish_dirs(plan, made)
            self.finished = True

    def _missing_room(self, files):
        """The error text when the files that are new on the target do not fit there, else "". Files that exist already may be skipped
        or replaced: they are not counted.
        A move counts only the files that come from another filesystem: the others are renamed and need no room. Without this a move to a
        stick that is too small would write until it is full, and a full target throws away every copy that still waits for its check.
        shortcut: a compressing filesystem is not allowed more than it reports free; the per-file reservation still stops that one."""
        if not files:
            return ""
        try:
            target = os.path.dirname(files[0][1])
            there, free = os.stat(target).st_dev, shutil.disk_usage(target).free
            # within one filesystem a move renames, and a copy without verification may be a reflink (btrfs, XFS): neither needs the room
            # shortcut: so such a copy is not checked where there are no reflinks (ext4) either; the per-file reservation stops it, and the files written stay
            need = sum(_room(src, size) for src, dst, kind, size in files if kind == "file" and not os.path.lexists(dst)
                       and not ((self.move or not self.verify) and os.lstat(src).st_dev == there))
        except OSError:
            return ""  # a filesystem that cannot say is not a reason to refuse
        if need <= free:
            return ""
        size = GLib.format_size if GLib else str  # core runs without PyGObject, the UI does not
        return _("Not enough space on the target: {need} needed, {free} free").format(need=size(need), free=size(free))

    def _make_dirs(self, plan, made):
        """Creates the folders; one that cannot be created (a file has its name) fails alone, without its content."""
        bad = []
        for src, dst, kind, _size in plan:
            if kind == "dir" and not dst.startswith(tuple(bad)) and not os.path.isdir(dst):  # inside a failed folder: no extra line
                try:
                    st = os.stat(src)  # its times now: a move changes them when the files leave
                    os.makedirs(dst)
                    made.append((src, dst, (st.st_atime_ns, st.st_mtime_ns)))
                except OSError as e:
                    bad.append(dst + os.sep)
                    self.results.append(Result(src, dst, "failed", e.strerror or str(e)))
        return [p for p in plan if not p[1].startswith(tuple(bad))]

    def _finish_dirs(self, plan, made):
        aborted = self.cancelled or self.fatal
        for src, dst, times in reversed(made):
            if aborted:
                with contextlib.suppress(OSError):
                    os.rmdir(dst)  # only succeeds while still empty
            else:
                with contextlib.suppress(OSError):  # best effort: FAT/SMB reject some metadata
                    shutil.copystat(src, dst)
                    os.utime(dst, ns=times)
        if self.move and not aborted:
            for src in sorted(set(self.dirs), key=len, reverse=True):  # inner folders first
                with contextlib.suppress(OSError):
                    os.rmdir(src)  # stays if files were skipped or failed

    def _scan(self):
        plan = []

        def walk(src, dst):
            try:
                st = os.lstat(src)
                mode = st.st_mode
                if stat.S_ISDIR(mode):
                    plan.append((src, dst, "dir", 0))
                    with os.scandir(src) as it:
                        for e in it:
                            walk(e.path, os.path.join(dst, e.name))
                elif name_key(dst).endswith(PART):  # a leftover of a hard stop; its name is the temp name of the file beside it
                    self.results.append(Result(src, dst, "skipped"))
                elif stat.S_ISLNK(mode):
                    plan.append((src, dst, "link", 0))
                elif stat.S_ISREG(mode):
                    plan.append((src, dst, "file", st.st_size))
                else:
                    self.results.append(Result(src, dst, "failed", _("Not a regular file (device, socket or pipe)")))
            except OSError as e:
                self.results.append(Result(src, dst, "failed", e.strerror or str(e)))

        self.results += self._there
        for src, dst in self.items:
            if not os.path.lexists(src):
                self.results.append(Result(src, dst, "failed", _("Not found")))
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            walk(src, dst)
        return plan

    def _advance(self, n, skipped=0):
        """Move the progress on by n units; negative takes back what a failed attempt had reported. `skipped`: the part of n that no byte moved for."""
        with self._progress_lock:
            self.done += n
            self._skipped += skipped

    def _checkpoint(self):
        """Where a worker waits while paused and stops after cancel(); called before every file and chunk."""
        self._run.wait()
        if self._cancel.is_set():
            raise Cancelled

    def _run_item(self, src, dst, kind, size):
        """Copy, move or skip one planned entry and record its Result. A cancel ends the entry without one."""
        try:
            self._checkpoint()
            if self.fatal:  # the target is full: no further file is started
                raise Cancelled
            verify = self.verify  # this file keeps the choice made now, even if it is switched meanwhile
            weight = size * (2 if verify else 1)
            with self._progress_lock:
                self._unstarted -= size
                self._committed += weight
                self._later += weight - size
            self.current = src
            target = self._resolve(src, dst)
            if target is None:
                self._advance(weight)
                status, error, digest = "skipped", "", ""
            else:
                dst = target
                status, error, digest = self._transfer(src, dst, kind, size, weight, verify)
            source = target = digest  # a copy that passed its check: both sums are the same
        except Cancelled:
            return
        except OSError as e:
            self._advance(weight)
            status, error, digest = "failed", e.strerror or str(e), ""
            source, target = getattr(e, "source", ""), getattr(e, "target", "")  # set by a bad copy: what was read and what was found
        result = Result(src, dst, status, error, size, digest, source=source, target=target)
        self.results.append(result)
        with self._progress_lock:
            if dst + PART in self._deferred:  # written to its temp file and hashed on the way: phase 2 reads it back
                self._to_check.append((result, self._deferred.pop(dst + PART), dst + PART))
                self._checks += size
            else:  # nothing waits for step 2
                self._later -= weight - size

    def _run_checks(self):
        """Phase 2, after every file is written: the copies waiting in their temp files are read back and renamed into place when they match,
        and the files copied unchecked, because verification was switched on later, are compared with their sources."""
        late = [r for r in self.results if r.status == "ok" and r.size and self.verify and not self.move]
        if not (self._to_check or late):
            return
        with self._progress_lock:  # the figures are set before the phase shows, so a tick never sees step 2 with step 1's base
            self.check_base = self.done
            self._committed += 2 * sum(r.size for r in late)  # source and copy are both read
        if self.verify:  # switched off meanwhile: the waiting copies are put in place unchecked, and there is no step 2 to show
            self.stage, self.check_pass = "verify", True
        with ThreadPoolExecutor(self.workers) as pool:
            list(pool.map(lambda c: self._check_one(*c), list(self._to_check)))
            list(pool.map(self._verify_late, late))

    def _drop_unchecked(self):
        """A cancelled or failed job: copies that were never checked do not get their name, and are no result."""
        unchecked = [c for c in self._to_check if c[0].status == "copied"]  # a check that ran changed the status; looked up once here, not after every file
        for _result, _digest, part in unchecked:
            with contextlib.suppress(OSError):
                os.unlink(part)
        gone = {id(c[0]) for c in unchecked}
        self.results[:] = [r for r in self.results if id(r) not in gone]
        self._to_check.clear()

    def _check_one(self, result, digest, part):
        """Phase 2 for one copy: read the temp file back and put it in place when it matches; a move then removes its source. Only the check
        decides: a bad copy never gets the name, and an older file there stays."""
        try:
            self._checkpoint()
            self.current = result.src
            self.phases[result.src] = "verify"
            self._finish_copy(result, digest, part)
        except Cancelled:
            return  # stays listed: _drop_unchecked removes the temp file
        except OSError as e:
            result.status, result.error = "failed", e.strerror or str(e)
            with contextlib.suppress(OSError):
                os.unlink(part)
        finally:
            self.phases.pop(result.src, None)
        with self._progress_lock:
            self._checks -= result.size
        self.late.append(result)

    def _finish_copy(self, result, digest, part):
        """The read-back, the retries of a bad copy and the rename of _check_one."""
        again = self._hash(part, self._advance, result.size) if self.verify else None  # None: switched off, nothing to compare
        if again is not None and again != digest:
            os.unlink(part)
            self._advance(-2 * result.size)  # the attempt is taken back, as a retry always started at zero
            for _attempt in range(RETRIES - 1):  # written again with its read-back, as a bad copy always was
                try:
                    result.digest = self._copy_once(result.src, result.dst, "file", result.size, True)
                    break
                except OSError as e:
                    error = e.strerror or str(e)
            else:
                self._advance(2 * result.size)
                result.status, result.error = "failed", error if RETRIES > 1 else _("Checksum mismatch after copy")
                result.source, result.target = digest.hex(), again.hex()
                return
        else:
            os.replace(part, result.dst)
            result.digest = digest.hex() if again is not None else ""
        result.source = result.target = result.digest
        result.status = "verified" if result.digest else "ok"
        if self.move and (problem := self._release(result.src, result.dst)):  # the copy is checked: now the source can go
            result.status, result.error = "failed", problem

    def _verify_late(self, r):
        """The check of a file that was copied unchecked: compare source and copy, and update the result in place."""
        try:
            self._checkpoint()
            self.current = r.src
            self.phases[r.src] = "verify"
            with open(r.dst, "rb") as f:
                os.fsync(f.fileno())  # written without a flush: the read-back must come from the disk, not the cache
            a, b = self._hash(r.src, self._advance, r.size), self._hash(r.dst, self._advance, r.size)
            if a is None or b is None:
                return  # switched off meanwhile: the file stays copied, unchecked
        except Cancelled:
            return
        except OSError as e:
            r.status, r.error = "failed", e.strerror or str(e)
        else:
            # shortcut: a bad copy that is already in place is reported, not removed; "Retry failed" replaces it (Result.kept)
            if a == b:
                r.status, r.error, r.digest = "verified", "", a.hex()
                r.source = r.target = r.digest
            else:
                r.status, r.error, r.digest, r.kept = "failed", _("Checksum mismatch after copy"), "", True
                r.source, r.target = a.hex(), b.hex()
        finally:
            self.phases.pop(r.src, None)
        self.late.append(r)

    def _resolve(self, src, dst):
        """Final target path for src, or None to skip it. Every target is claimed, so two sources with one name
        (search results, clipboard) keep both instead of silently overwriting each other."""
        target, fresh = self._decide(src, dst)
        if target is None:
            return None
        with self._claim_lock:
            if fresh or name_key(target) in self._claimed:
                target = free_name(target, self._claimed)
            self._claimed.add(name_key(target))
        return target

    def _decide(self, src, dst):
        """(target, needs a new name): the policy applied to what is in the way."""
        exists = os.path.lexists(dst)  # looked at first: a file of this job claims its target before it creates it
        if name_key(dst) in self._claimed:
            return dst, True  # taken by another file of this job: no question to ask, nothing may be overwritten
        if dst in self.replace:
            return dst, False  # our own bad copy from the run before
        if not exists:
            return dst, False
        if name_key(dst) in self._sources:
            return dst, True  # a source of this job lies there (a folder moved into itself, say): beside it, never over it
        if os.path.isdir(dst) and not os.path.islink(dst):
            raise IsADirectoryError(errno.EISDIR, _("A folder with this name already exists"))
        s, d = os.lstat(src), os.lstat(dst)
        with self._ask_lock:  # one dialog at a time, and "apply to all" must hold for the next waiter
            self._checkpoint()  # a Cancel while this worker waited for the lock must not open another dialog
            policy = self.policy
            if policy == "ask":
                answer = self.ask(src, dst) if self.ask else None
                policy, remember = answer or ("skip", False)
                if remember:
                    self.policy = policy
        if policy == "cancel":
            self.cancel()
            raise Cancelled
        if policy == "skip":
            return None, False
        if policy == "newer":
            return (dst, False) if s.st_mtime > d.st_mtime else (None, False)
        return dst, policy == "keep_both"

    # --- the checksum file: collected after the job, the window and the terminal decide what goes in -----------------------------
    def sum_files(self):
        """The finished regular files of this job, which a checksum file can list."""
        return [r for r in self.results if r.status in ("ok", "verified") and os.path.isfile(r.dst) and not os.path.islink(r.dst)]

    def sum_gap(self):
        """(the finished files that have no checksum for the file, their bytes): copied before it was switched on, or only renamed."""
        gap = [r for r in self.sum_files() if r.dst + PART not in self._sums]
        return gap, sum(r.size for r in gap)

    def sum_target(self):
        """(the folder the paths in the file are relative to, the file's path): in the target folder, named as HashTally names its own."""
        base = os.path.commonpath([os.path.dirname(dst) for _src, dst in self.items])
        _hasher, several, ending, _size = SUM_ALGOS[self.sums]
        return base, os.path.join(base, several if len(self.items) > 1 else os.path.basename(self.items[0][1]) + ending)

    def sum_entries(self, read_gap):
        """{path relative to the target folder: hex}, sorted: the files that have a checksum, and with `read_gap` the others too, read from their copies
        (progress in sum_read; a file that cannot be read is left out). Call it from a thread when the gap is big."""
        base = self.sum_target()[0]
        files = self.sum_files()
        found = {os.path.relpath(r.dst, base): self._sums[r.dst + PART] for r in files if r.dst + PART in self._sums}
        if read_gap:
            def read(r):
                try:
                    return os.path.relpath(r.dst, base), self._sum_of(r.dst)
                except OSError:
                    return None
            with ThreadPoolExecutor(self.workers) as pool:
                found.update(x for x in pool.map(read, self.sum_gap()[0]) if x)
        return dict(sorted(found.items(), key=lambda kv: kv[0].casefold()))

    def _sum_of(self, path):
        h = SUM_ALGOS[self.sums][0]()
        with open(path, "rb") as f:
            os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)  # from the disk, as a read-back is
            while data := f.read(CHUNK):
                h.update(data)
                with self._progress_lock:
                    self.sum_read += len(data)
        return h.digest().hex()

    def sum_existing(self):
        """(path, {entries}, mtime) of the checksum file that is in the way, else None."""
        path = self.sum_target()[1]
        try:
            st = os.stat(path)
            return path, parse_sums(self.sums, Path(path).read_text(encoding="utf-8", errors="replace")), st.st_mtime
        except OSError:
            return None

    def write_sums(self, entries, how="keep_both"):
        """Writes the checksum file and returns its path. `how` if one is there already: update (its entries for these files are replaced, the
        others stay), keep_both (a free name), overwrite. It is written to a temp file and renamed, so a crash leaves the old one."""
        path = self.sum_target()[1]
        if os.path.lexists(path):
            if how == "keep_both":
                path = free_name(path)
            elif how == "update":
                entries = {**parse_sums(self.sums, Path(path).read_text(encoding="utf-8", errors="replace")), **entries}
        part = path + PART
        with open(part, "w", encoding="utf-8", newline="\n", errors="surrogateescape") as f:
            f.write(format_sums(self.sums, entries))
            f.flush()
            os.fsync(f.fileno())
        os.replace(part, path)
        return path

    def _transfer(self, src, dst, kind, size, weight, verify):
        """Returns (status, error, digest). Only Cancelled escapes."""
        if self.move:
            renamed = self._rename(src, dst, weight)
            if renamed:
                return renamed
        error = ""
        for _attempt in range(RETRIES):
            try:
                digest = self._copy_once(src, dst, kind, size, verify, defer=verify)
                break
            except OSError as e:
                error = e.strerror or str(e)
                if e.errno in FULL:  # no cancel: the copies that are written still get their check and their name, as they would without verification
                    self.fatal = error
                    self._advance(weight)
                    return "failed", error, ""
        else:
            self._advance(weight)
            return "failed", error, ""
        if dst + PART in self._deferred:  # checked in phase 2, which also removes the source of a move
            return "copied", "", ""
        if self.move and (problem := self._release(src, dst)):
            return "failed", problem, ""
        return ("verified" if digest else "ok"), "", digest

    def _release(self, src, dst):
        """The end of a move that copied: the source goes only once the copy's name is on the disk, and only while it is
        still the file that was read. Returns why it stays, or "" when it is gone."""
        try:
            _sync_dir(os.path.dirname(dst))  # never drop this for speed: without it a crash after the unlink can leave the copy only as name.trustpart
            read = self._stamps.pop(dst + PART, None)  # None: a link, no data was read
            # one at a time: a source that is in the job twice is removed by one worker while the other looks at it, and a file being
            # removed shows a new ctime for a moment. Gone already: that is what a move wants
            with self._release_lock, contextlib.suppress(FileNotFoundError):
                if read and read != _stamp(os.lstat(src)):  # saved by an app after it was read (phase 2 can come hours later)
                    return _("Copied, but the source was kept: it was changed after it was read")
                os.unlink(src)
        except OSError as e:
            return _("Copied, but the source could not be removed: {error}").format(error=e.strerror)
        return ""

    def _rename(self, src, dst, weight):
        """A move within one filesystem is a rename: instant, nothing to verify. Returns the result of _transfer,
        or None where a copy is needed (another filesystem)."""
        try:
            os.replace(src, dst)
        except OSError as e:
            if e.errno == errno.EXDEV:
                return None
            self._advance(weight)
            return "failed", e.strerror or str(e), ""
        self._advance(weight)
        return "ok", "", ""

    def _copy_once(self, src, dst, kind, size, verify, defer=False):
        """One attempt at the copy; returns its checksum. A failed attempt takes back the progress it reported, so a retry starts at zero."""
        got, skipped = [0], [0]

        def tick(n, skip=False):
            got[0] += n
            skipped[0] += n if skip else 0
            self._advance(n, n if skip else 0)

        try:
            return self._copy(src, dst + PART, dst, kind, size, verify, tick, defer)
        except OSError:
            self._advance(-got[0], -skipped[0])
            raise

    def _copy(self, src, part, dst, kind, size, verify, tick, defer=False):
        """Write to a temp file, verify, then rename into place: a cancel, crash or bad
        copy never leaves a half-written file under the real name or damages an old one.
        With `defer` the read-back and the rename are left to phase 2 (_deferred holds the source digest).
        Returns the hex checksum, or "" when nothing was hashed."""
        hexdigest = ""
        with contextlib.suppress(FileNotFoundError):
            os.unlink(part)  # a stale part or a planted link: never write through it
        try:
            if kind == "link":
                try:
                    os.symlink(os.readlink(src), part)
                except OSError as e:
                    if e.errno in (errno.EPERM, errno.EOPNOTSUPP):  # what FAT and exFAT answer; shortcut: a locked folder says EPERM too, the text then misleads
                        raise OSError(e.errno, _("The target file system cannot store links")) from e
                    raise
            else:
                self.phases[src] = "copy"
                digest = self._write(src, part, size, verify, tick)
                if digest and not self.verify:  # switched off while this file was written: skip its read-back
                    tick(size, True)  # the pass was promised in the progress total; no byte moves for it
                elif digest and defer:
                    self._deferred[part] = digest
                elif digest:
                    self.phases[src] = "verify"
                    again = self._hash(part, tick, size)
                    if again == digest:
                        hexdigest = digest.hex()
                    elif again is not None:  # None: switched off during the read-back, the file counts as copied without a check
                        bad = OSError(errno.EIO, _("Checksum mismatch after copy"))
                        bad.source, bad.target = digest.hex(), again.hex()  # the failed row shows both
                        raise bad
                with contextlib.suppress(OSError):  # best effort: FAT/SMB reject some metadata
                    shutil.copystat(src, part)
                if part in self._deferred:
                    return ""  # phase 2 checks the temp file and renames it
            # shortcut: a file that appears at dst after the conflict check (a share, a colleague) is replaced without asking; os.link would catch it but fails on FAT/SMB, upgrade if it bites
            os.replace(part, dst)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(part)
            raise
        finally:
            self.phases.pop(src, None)
        return hexdigest

    def _write(self, src, part, size, verify, tick):
        main = self._hasher() if verify else None
        side = SUM_ALGOS[self.sums][0]() if self.sums else None  # this file keeps the choice made now
        h = _Tee(main, side) if side else main  # with a checksum file there is no reflink and no kernel copy: the bytes must be seen
        # created with the source's mode (less the umask), as cp does: a private file must not be readable by others while it is written
        with open(src, "rb") as fi, open(part, "xb", opener=lambda path, flags: os.open(path, flags, stat.S_IMODE(os.fstat(fi.fileno()).st_mode))) as fo:
            if self.move:
                self._stamps[part] = _stamp(os.fstat(fi.fileno()))
            if h is None:  # speed boost: instant reflink on btrfs/XFS
                try:
                    fcntl.ioctl(fo.fileno(), FICLONE, fi.fileno())
                    tick(size)
                    self._written(size)
                    return None
                except OSError:
                    pass  # no reflink here, copy normally
            sparse = _data_extents(fi.fileno())
            if sparse:  # holes stay holes: only the parts with data are read and written, each at its place
                self._pump_sparse(fi, fo, tick, h, bool(main or self.move), *sparse)
            else:
                reserved = _reserve(fo.fileno(), size)
                self._pump(fi, fo, tick, h, bool(main or self.move))
                if reserved:
                    fo.truncate()  # the source may have shrunk since it was measured: no zeros at the end
            if not (main or self.move):  # verifying and moving: _pump has put every byte on the target already, a second flush only costs time
                with self.writing:  # "done" means on the target: a failing disk must not pass as a copy
                    try:
                        fo.flush()
                        os.fsync(fo.fileno())
                    except OSError as e:
                        if e.errno not in NO_SYNC:
                            raise
                        # shortcut: a target that cannot flush (some FUSE/SMB mounts) is trusted as unflushed, upgrade if one loses data
        if side:
            self._sums[part] = side.digest().hex()
        return main.digest() if main else None

    def _pump(self, fi, fo, tick, h, syncing):
        self._copy_range(fi, fo, tick, h, syncing)

    def _pump_sparse(self, fi, fo, tick, h, syncing, extents, size):
        """_pump for a file with holes. `extents` are its parts with data; what lies between them is a hole and stays one on the target (the file is
        cut to its length at the end, which makes the last hole). The checksums see a hole as the zeros it reads as, and the progress takes a hole
        as done at once, like a skipped part: no byte moves for it."""
        zeros, pos = bytes(CHUNK), 0

        def hole(n):
            while n > 0:
                self._checkpoint()
                m = min(n, CHUNK)
                if h:
                    h.update(zeros[:m])
                tick(m, True)
                n -= m

        for start, length in extents:
            hole(start - pos)
            _reserve(fo.fileno(), length, start)  # this part only: the holes must not be claimed
            fi.seek(start)
            fo.seek(start)
            self._copy_range(fi, fo, tick, h, syncing, length)
            pos = start + length
        hole(size - pos)
        fo.truncate(size)

    def _copy_range(self, fi, fo, tick, h, syncing, left=None):
        """Copies fi to fo, to the end of fi or, with `left`, that many bytes. Verifying and moving flush in small steps and at the end, so a byte counts as done when it is on the target (a move deletes the source next, a verify reads the copy back). A plain copy fills the cache at full speed (the graph shows that) and _write flushes once at the end."""
        kernel = h is None and left is None and hasattr(os, "copy_file_range")  # in-kernel copy skips userspace (Linux only); hashing needs to see the bytes
        # syncing: _write flushes in these cases anyway (verifying, moving)
        pending, step, last = 0, 4 << 20, time.monotonic()
        while True:
            self._checkpoint()
            synced = False
            with self.writing:  # only the I/O: a pause does not count
                if kernel:
                    try:
                        n = os.copy_file_range(fi.fileno(), fo.fileno(), CHUNK)
                    except OSError as e:
                        if e.errno not in NO_KERNEL_COPY:
                            raise
                        kernel = False
                        continue
                else:
                    data = fi.read(CHUNK if left is None else min(CHUNK, left))
                    n = len(data)
                    if h:
                        h.update(data)
                    fo.write(data)
                if left is not None:
                    left -= n
                end = not n or left == 0
                pending += n
                if syncing and pending and (pending >= step or end):
                    fo.flush()
                    os.fdatasync(fo.fileno())
                    synced = True
            if synced:
                now = time.monotonic()
                step = int(min(max(pending / max(now - last, 0.001) * 0.2, SYNC_MIN), SYNC_MAX))
                last = now
            if pending and (synced or not syncing):
                tick(pending)
                self._written(pending)
                pending = 0
            if end:
                return

    def _written(self, n):
        with self._progress_lock:
            self.written += n

    def _hash(self, path, tick, size=None):
        """The checksum of a file. With `size`, the read-back of a fresh copy: when verification is switched off meanwhile
        it stops at once, counts the unread rest as done and returns None."""
        h, read = self._hasher(), 0
        with open(path, "rb") as f:
            # DONTNEED drops only clean pages: the fsync every copy gets before its read-back is what sends this read to the disk
            # (measured with mincore on ext4: 0 pages left after fsync, all of them without). Never drop that fsync for speed.
            # shortcut: NFS/SMB/FUSE client caches and a drive's own write cache may still answer; no cure without O_DIRECT, which tmpfs and FUSE refuse
            os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            sparse = _data_extents(f.fileno())
            parts, zeros = [(0, None, False)], bytes(CHUNK)  # (where to start, how much (None: to the end), whether it is a hole)
            if sparse:  # holes are not read: they hash as the zeros they read as, and count as done at once, but not as speed
                extents, length = sparse
                parts, pos = [], 0
                for start, count in extents:
                    if start > pos:
                        parts.append((pos, start - pos, True))
                    parts.append((start, count, False))
                    pos = start + count
                if length > pos:
                    parts.append((pos, length - pos, True))
            for start, left, is_hole in parts:
                f.seek(start)
                while left is None or left > 0:
                    self._checkpoint()
                    if size is not None and not self.verify:
                        tick(max(size - read, 0))
                        return None
                    if is_hole:
                        data = zeros[:min(left, CHUNK)]
                    else:
                        with self.reading:
                            data = f.read(CHUNK if left is None else min(CHUNK, left))
                        if not data:
                            break
                    h.update(data)
                    read += len(data)
                    tick(len(data))
                    if left is not None:
                        left -= len(data)
                    if not is_hole:
                        with self._progress_lock:
                            self.readback += len(data)
            return h.digest()
