# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DigitGram
"""Self-check for src/core.py, src/i18n.py and po/de.po. Run: python3 tests/test_core.py"""
import ast
import contextlib
import ctypes
import errno
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
import cli  # noqa: E402
import core  # noqa: E402
import desktop  # noqa: E402
import i18n  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.src, self.dst = Path(tmp.name, "src"), Path(tmp.name, "dst")
        self.src.mkdir()
        self.dst.mkdir()

    def write(self, rel, data, base=None):
        p = Path(base or self.src, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def job(self, sources, job_class=core.Job, **kw):
        job = job_class(core.items_for([str(s) for s in sources], str(self.dst)), **kw)
        job.run()
        return job

    def status(self, job):
        return {Path(r.src).name: r.status for r in job.results}

    def assertClean(self):
        self.assertEqual(list(self.dst.rglob("*" + core.PART)), [])


class Corrupting(core.Job):
    """Verification fails the first `bad` times, as if the disk returned wrong data."""
    bad = 1

    def _hash(self, path, tick, size=None):
        type(self).bad -= 1
        if type(self).bad < 0:
            return super()._hash(path, tick, size)
        tick(size or 0)  # the read-back happened, it just returned the wrong data
        return b"wrong"


class CopyTests(Base):
    def test_tree_is_copied_and_verified(self):
        big = os.urandom(9 << 20)  # crosses chunk boundaries
        self.write("a/one.txt", b"hello")
        self.write("a/b/two.bin", big)
        (self.src / "a/empty").mkdir()
        os.symlink("one.txt", self.src / "a/link")
        job = self.job([self.src / "a"])
        self.assertEqual((self.dst / "a/b/two.bin").read_bytes(), big)
        self.assertEqual((self.dst / "a/one.txt").read_bytes(), b"hello")
        self.assertTrue((self.dst / "a/empty").is_dir())
        self.assertEqual(os.readlink(self.dst / "a/link"), "one.txt")
        self.assertEqual(self.status(job), {"one.txt": "verified", "two.bin": "verified", "link": "ok"})
        self.assertEqual((job.done, job.failed, job.finished), (job.total, 0, True))
        self.assertClean()

    def test_every_checksum_algorithm_verifies(self):
        data = os.urandom(3 << 20)
        for algo in core.ALGOS:
            with self.subTest(algo=algo):
                f = self.write(f"{algo}.bin", data)
                job = self.job([f], algo=algo)
                self.assertEqual(self.status(job), {f"{algo}.bin": "verified"})
                self.assertEqual(job.size, len(data))

    def test_crc32_notices_corruption(self):
        class Flip(core.Job):
            def _write(self, src, part, size, verify, tick):
                digest = super()._write(src, part, size, verify, tick)
                with open(part, "r+b") as f:  # a bit flips on its way to the disk
                    f.write(b"\xff")
                return digest

        f = self.write("f.bin", b"\x00" * 4096)
        job = self.job([f], job_class=Flip, algo="crc32")
        self.assertEqual(self.status(job), {"f.bin": "failed"})
        self.assertFalse((self.dst / "f.bin").exists())
        self.assertClean()

    def test_each_step_has_its_own_progress(self):
        seen = []

        class Watch(core.Job):
            def _run_checks(self):
                seen.append(("end of step 1", self.step_fraction, self.first_pass * 2 == self.total))
                super()._run_checks()

            def _transfer(self, *a):
                result = super()._transfer(*a)
                seen.append(("written", self.step_fraction, True))  # a file written but not yet registered: the writing of everything started is done
                return result

            def _check_one(self, *a):
                seen.append(("start of step 2", self.step_fraction, True))
                super()._check_one(*a)

        self.write("a.bin", os.urandom(300_000))
        self.write("b.bin", os.urandom(200_000))
        job = self.job([self.src / "a.bin", self.src / "b.bin"], job_class=Watch, workers=1)
        self.assertAlmostEqual(seen[0][1], 300 / 500)  # not 0.3: the first file's read-back is no part of step 1
        self.assertEqual(seen[1], ("written", 1.0, True))
        self.assertEqual(seen[2], ("end of step 1", 1.0, True))  # writing is half of the units
        self.assertEqual(seen[3], ("start of step 2", 0.0, True))
        self.assertEqual((job.step_fraction, job.step_left), (1.0, 0))  # the time remaining counts the step in view, not the whole job
        self.assertEqual(self.job([self.write("c.bin", b"c" * 99)], verify=False).step_fraction, 1.0)
        moved = self.job([self.write("d.bin", b"d" * 99)], move=True)  # a rename waits for no step 2
        self.assertEqual((moved.first_pass, moved.step_fraction), (moved.total, 1.0))

    def test_copy_without_verify(self):
        data = os.urandom(5 << 20)
        f = self.write("f.bin", data)
        job = self.job([f], verify=False)
        self.assertEqual((self.dst / "f.bin").read_bytes(), data)
        self.assertEqual(self.status(job), {"f.bin": "ok"})
        self.assertEqual(job.done, job.total)

    def test_metadata_is_kept(self):
        f = self.write("f.txt", b"x")
        os.utime(f, (1_000_000_000, 1_000_000_000))
        self.job([f])
        self.assertEqual(int((self.dst / "f.txt").stat().st_mtime), 1_000_000_000)

    def test_bad_copy_is_retried(self):
        Corrupting.bad = 1
        f = self.write("f.txt", b"data" * 1000)
        job = self.job([f], job_class=Corrupting)
        self.assertEqual(self.status(job), {"f.txt": "verified"})
        self.assertEqual(job.done, job.total)  # failed attempt was rolled back
        self.assertTrue(job.results[0].source and job.results[0].source == job.results[0].target == job.results[0].digest)  # a good copy: one sum, twice
        self.assertClean()

    def test_permanently_bad_copy_fails_and_keeps_old_file(self):
        Corrupting.bad = 99
        old = self.write("f.txt", b"old", base=self.dst)
        os.utime(old, (1, 1))
        f = self.write("f.txt", b"new content")
        job = self.job([f], job_class=Corrupting, policy="overwrite")
        self.assertEqual(self.status(job), {"f.txt": "failed"})
        r = job.results[0]
        self.assertTrue(r.source and r.target and r.source != r.target and not r.digest)  # a bad copy shows both sums, so one can compare them
        self.assertEqual(old.read_bytes(), b"old")
        self.assertClean()

    def test_folder_into_itself_is_refused(self):
        self.write("a/x", b"1")
        with self.assertRaises(ValueError):
            core.Job(core.items_for([str(self.src / "a")], str(self.src / "a")))

    def test_a_move_onto_itself_is_listed_as_skipped(self):
        f = self.write("a.txt", b"a")
        job = core.Job(core.items_for([str(f)], str(self.src)), move=True)  # into the folder it is in
        job.run()
        self.assertEqual(([(r.src, r.status) for r in job.results], f.read_bytes(), job.fatal), ([(str(f), "skipped")], b"a", ""))

    def test_copy_onto_itself_keeps_both(self):
        f = self.write("x.txt", b"1")
        core.Job(core.items_for([str(f)], str(self.src))).run()
        self.assertEqual((self.src / "x (2).txt").read_bytes(), b"1")

    def test_unreadable_file_fails_alone(self):
        self.write("ok.txt", b"1")
        bad = self.write("bad.txt", b"2")
        bad.chmod(0)
        if os.access(bad, os.R_OK):
            self.skipTest("running as root, chmod 0 does not block reads")
        job = self.job([self.src / "ok.txt", bad])
        self.assertEqual(self.status(job), {"ok.txt": "verified", "bad.txt": "failed"})

    def test_disk_full_stops_the_job(self):
        f = self.write("f.txt", b"1")
        with mock.patch.object(core.Job, "_write", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            job = self.job([f])
        self.assertEqual(job.fatal, "No space left on device")
        self.assertClean()


class ReservationTests(Base):
    def test_a_target_that_fills_up_keeps_the_files_that_fit(self):
        class Full(core.Job):
            def _write(self, src, part, size, verify, tick):
                if Path(src).name >= "f7":  # the room ends here; "f9" must not even be tried
                    tried.append(Path(src).name)
                    raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
                return super()._write(src, part, size, verify, tick)

        for kw in ({}, {"move": True}, {"verify": False}):
            with self.subTest(kw=kw), CrossDevice():
                tried, files = [], [self.write(f"f{i}", b"x" * 1000) for i in range(10)]
                job = self.job(files, job_class=Full, workers=1, policy="overwrite", **kw)
                done = "ok" if kw.get("verify") is False else "verified"
                self.assertEqual((job.fatal, tried), (os.strerror(errno.ENOSPC), ["f7"]))
                self.assertEqual(self.status(job), {**{f"f{i}": done for i in range(7)}, "f7": "failed"})
                self.assertEqual(sorted(p.name for p in self.dst.iterdir()), [f"f{i}" for i in range(7)])  # checked and named, not thrown away
                self.assertEqual([f.exists() for f in files], [not kw.get("move")] * 7 + [True] * 3)  # a move lets go of what arrived, nothing else

    def refuse(self, code):
        def fallocate(fd, mode, offset, length):
            ctypes.set_errno(code)
            return -1
        return mock.patch.object(core, "_fallocate", fallocate)

    def test_a_full_target_stops_before_a_byte_is_written(self):
        f = self.write("big.bin", b"x" * 100_000)
        with self.refuse(errno.ENOSPC):
            job = self.job([f], workers=1)
        self.assertEqual((job.fatal, job.written), ("No space left on device", 0))
        self.assertClean()

    def test_a_target_too_small_for_the_job_gets_nothing(self):
        files = [self.write(f"{i}.bin", b"x" * 1000) for i in range(3)]
        self.write("0.bin", b"old", base=self.dst)  # exists already: skipped or replaced, so not counted

        def free(n):
            return mock.patch.object(core.shutil, "disk_usage", return_value=types.SimpleNamespace(free=n))
        with free(1999):
            job = self.job(files, policy="skip")
        self.assertTrue(job.fatal.startswith("Not enough space"), job.fatal)
        self.assertEqual((job.results, os.listdir(self.dst)), ([], ["0.bin"]))
        with free(2000):
            job = self.job(files, policy="skip")
        self.assertEqual((job.fatal, sorted(os.listdir(self.dst))), ("", ["0.bin", "1.bin", "2.bin"]))
        with free(0):  # a move may be a rename: no room needed
            self.assertEqual(self.job([self.write("m.bin", b"x")], move=True).fatal, "")

    def test_a_move_to_another_filesystem_that_is_too_small_gets_nothing(self):
        here = os.stat(self.src).st_dev
        other = next((d for d in ("/dev/shm", "/var/tmp", "/tmp") if os.path.isdir(d) and os.access(d, os.W_OK) and os.stat(d).st_dev != here), None)
        if not other:
            self.skipTest("no second filesystem to move to")
        tmp = tempfile.TemporaryDirectory(dir=other)
        self.addCleanup(tmp.cleanup)
        self.dst = Path(tmp.name)
        files = [self.write(f"{i}.bin", b"x" * 1000) for i in range(3)]
        with mock.patch.object(core.shutil, "disk_usage", return_value=types.SimpleNamespace(free=2999)):
            job = self.job(files, move=True)
        self.assertTrue(job.fatal.startswith("Not enough space"), job.fatal)
        self.assertEqual((job.written, os.listdir(self.dst), all(f.exists() for f in files)), (0, [], True))
        with mock.patch.object(core.shutil, "disk_usage", return_value=types.SimpleNamespace(free=3000)):
            job = self.job(files, move=True)
        self.assertEqual((job.fatal, set(self.status(job).values()), any(f.exists() for f in files)), ("", {"verified"}, False))

    def test_a_copy_that_may_be_a_reflink_is_not_refused_for_room(self):
        f = self.write("a.bin", b"x" * 1000)  # source and target are on one filesystem here
        with mock.patch.object(core.shutil, "disk_usage", return_value=types.SimpleNamespace(free=0)):
            self.assertEqual(self.job([f], verify=False).fatal, "")  # btrfs and XFS share the blocks: no room needed
            (self.dst / "a.bin").unlink()
            self.assertTrue(self.job([f], verify=True).fatal.startswith("Not enough space"))  # verifying writes every byte

    def test_a_moved_folder_keeps_its_date(self):
        for move in (True, False):
            with self.subTest(move=move):
                self.write("album/a.jpg", b"x")
                os.utime(self.src / "album", (1_000_000_000, 1_000_000_000))
                with CrossDevice():
                    self.job([self.src / "album"], move=move)
                self.assertEqual(int((self.dst / "album").stat().st_mtime), 1_000_000_000)  # not the moment its files left
                shutil.rmtree(self.dst / "album")

    def test_a_full_quota_stops_the_job_like_a_full_disk(self):
        files = [self.write(f"{i}.bin", b"x" * 1000) for i in range(3)]
        with mock.patch.object(core.Job, "_write", side_effect=OSError(errno.EDQUOT, "Disk quota exceeded")) as write:
            job = self.job(files, workers=1)
        self.assertEqual((job.fatal, write.call_count), ("Disk quota exceeded", 1))  # not three attempts for each of the files
        with self.refuse(errno.EDQUOT):
            self.assertEqual(self.job(files, workers=1).fatal, os.strerror(errno.EDQUOT))

    def test_a_file_too_big_for_the_filesystem_fails_before_a_byte_is_written(self):
        big, small = self.write("big.bin", b"x" * 100_000), self.write("small.bin", b"x")  # FAT32 refuses to reserve more than 4 GiB

        def fallocate(fd, mode, offset, length):
            if length < 50_000:
                return 0
            ctypes.set_errno(errno.EFBIG)
            return -1
        with mock.patch.object(core, "_fallocate", fallocate):
            job = self.job([big, small], workers=1)
        self.assertEqual((job.fatal, job.written, self.status(job)), ("", 1, {"big.bin": "failed", "small.bin": "verified"}))  # only that file: smaller ones still fit
        self.assertEqual(job.results[0].error, os.strerror(errno.EFBIG))
        self.assertClean()

    def test_a_private_file_is_never_readable_by_others_while_it_is_written(self):
        modes = []

        class Watch(core.Job):
            def _pump(self, fi, fo, tick, h):
                modes.append(stat.S_IMODE(os.fstat(fo.fileno()).st_mode))
                return super()._pump(fi, fo, tick, h)

        f = self.write("secret.key", b"x" * 1000)
        f.chmod(0o600)
        for verify in (True, False):
            with mock.patch.object(core.fcntl, "ioctl", side_effect=OSError(errno.EOPNOTSUPP, "")):  # a reflink would skip the writing
                self.job([f], job_class=Watch, verify=verify, policy="overwrite")
        self.assertEqual((modes, stat.S_IMODE((self.dst / "secret.key").stat().st_mode)), ([0o600, 0o600], 0o600))

    def test_where_the_filesystem_cannot_reserve_the_copy_goes_on(self):
        f = self.write("a.bin", b"x" * 100_000)
        for code in (errno.EOPNOTSUPP, errno.ENOSYS, errno.EINVAL):
            with self.subTest(code=code), self.refuse(code):
                job = self.job([f], workers=1, policy="overwrite")
                self.assertEqual((job.fatal, self.status(job)), ("", {"a.bin": "verified"}))

    def test_the_room_is_claimed_without_growing_the_file(self):
        # mode 0 would make FAT write the whole file full of zeros before the first byte of data
        modes = []

        def fallocate(fd, mode, offset, length):
            modes.append(mode)
            return 0

        with mock.patch.object(core, "_fallocate", fallocate):
            self.assertTrue(core._reserve(0, 100))
        self.assertEqual(modes, [1])  # FALLOC_FL_KEEP_SIZE, written out so the constant cannot drift with its test

    def test_a_source_that_shrank_leaves_no_zeros_at_the_end(self):
        f = self.write("a.bin", b"x" * 1000)
        job = core.Job([])
        for verify in (True, False):
            part = str(self.dst / f"a{verify}.part")
            with mock.patch.object(core.fcntl, "ioctl", side_effect=OSError(errno.EOPNOTSUPP, "")):  # no reflink: it would skip the reservation
                job._write(str(f), part, 1_000_000, verify, lambda n: None)  # measured before it shrank
            self.assertEqual(Path(part).read_bytes(), b"x" * 1000)
            self.assertLess(os.stat(part).st_blocks * 512, 500_000)  # the room reserved beyond the data is given back; half of it also fits 64 KiB blocks


class ConflictTests(Base):
    def setUp(self):
        super().setUp()
        self.new = self.write("f.txt", b"new!")
        self.old = self.write("f.txt", b"old", base=self.dst)
        os.utime(self.old, (1, 1))  # clearly older, and not "identical"

    def test_skip(self):
        job = self.job([self.new], policy="skip")
        self.assertEqual((self.status(job), self.old.read_bytes()), ({"f.txt": "skipped"}, b"old"))

    def test_overwrite(self):
        self.job([self.new], policy="overwrite")
        self.assertEqual(self.old.read_bytes(), b"new!")

    def test_keep_both(self):
        self.job([self.new], policy="keep_both")
        self.assertEqual(self.old.read_bytes(), b"old")
        self.assertEqual((self.dst / "f (2).txt").read_bytes(), b"new!")

    def test_newer_only(self):
        self.job([self.new], policy="newer")
        self.assertEqual(self.old.read_bytes(), b"new!")
        os.utime(self.new, (1, 1))
        os.utime(self.old, (5, 5))
        self.job([self.new], policy="newer")
        self.assertEqual(self.old.read_bytes(), b"new!")  # old one was newer, untouched
        os.utime(self.new, (5, 5))
        self.old.write_bytes(b"kept")
        os.utime(self.old, (5, 5))
        self.job([self.new], policy="newer")
        self.assertEqual(self.old.read_bytes(), b"kept")  # the same age is not newer

    def test_identical_file_is_asked_about_too(self):
        self.job([self.new], policy="overwrite")  # copystat makes mtime equal
        ask = mock.Mock(return_value=("skip", False))
        job = self.job([self.new], ask=ask)
        ask.assert_called_once()
        self.assertEqual(self.status(job), {"f.txt": "skipped"})

    def test_ask_with_apply_to_all(self):
        self.write("g.txt", b"new-g")
        os.utime(self.write("g.txt", b"old-g", base=self.dst), (1, 1))
        ask = mock.Mock(return_value=("skip", True))
        job = self.job([self.new, self.src / "g.txt"], ask=ask, workers=1)
        ask.assert_called_once()
        self.assertEqual(set(self.status(job).values()), {"skipped"})

    def test_cancel_asks_no_further_question(self):
        for n in range(3):
            self.write(f"h{n}.txt", b"new")
            self.write(f"h{n}.txt", b"old", base=self.dst)
        calls = []

        def ask(s, d):
            calls.append(s)
            time.sleep(0.2)  # the other workers queue up behind this dialog
            return "cancel", False

        job = self.job([self.src / f"h{n}.txt" for n in range(3)], ask=ask, workers=3)
        self.assertEqual(len(calls), 1)
        self.assertTrue(job.cancelled)

    def test_cancel_from_dialog(self):
        job = self.job([self.new], ask=lambda s, d: ("cancel", False))
        self.assertTrue(job.cancelled)
        self.assertEqual(self.old.read_bytes(), b"old")


class SpeedStats(Base):
    def test_clock_counts_overlapping_threads_once(self):
        clock = core.Clock()

        def work():
            with clock:
                time.sleep(0.1)

        threads = [threading.Thread(target=work) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertAlmostEqual(clock.secs, 0.1, delta=0.07)  # not 0.3

    def test_bytes_written_and_read_back(self):
        self.write("a", b"12345")
        self.write("b", b"123")
        job = self.job([self.src / "a", self.src / "b"])
        self.assertEqual((job.written, job.readback), (8, 8))
        self.assertGreater(job.writing.secs, 0)
        self.assertGreater(job.reading.secs, 0)


class MoveTests(Base):
    def test_move_on_same_filesystem(self):
        self.write("d/a.txt", b"a")
        self.write("d/sub/b.txt", b"b")
        job = self.job([self.src / "d"], move=True)
        self.assertEqual((self.dst / "d/sub/b.txt").read_bytes(), b"b")
        self.assertFalse((self.src / "d").exists())
        self.assertEqual((job.failed, job.written), (0, 0))  # a rename writes no data

    def test_a_move_that_cannot_rename_for_another_reason_than_the_filesystem_fails_and_keeps_the_source(self):
        real = os.replace

        def refuse(a, b):
            if not str(a).endswith(core.PART):
                raise PermissionError(errno.EACCES, "Permission denied")
            return real(a, b)

        with mock.patch("os.replace", refuse):
            job = self.job([self.write("a", b"x")], move=True, workers=1)  # copying instead would be a silent change of method
        self.assertEqual(self.status(job), {"a": "failed"})
        self.assertTrue((self.src / "a").exists() and not (self.dst / "a").exists())

    def test_move_across_filesystems_copies_verifies_then_deletes(self):
        f = self.write("f.txt", b"payload")
        with CrossDevice():
            job = self.job([f], move=True)
        self.assertEqual(self.status(job), {"f.txt": "verified"})
        self.assertEqual((self.dst / "f.txt").read_bytes(), b"payload")
        self.assertFalse(f.exists())

    def test_move_without_verification_flushes_the_copy_before_the_source_goes(self):
        f = self.write("f.txt", b"payload")
        with CrossDevice(), mock.patch.object(core.os, "fdatasync") as flush, \
                mock.patch.object(core.fcntl, "ioctl", side_effect=OSError(errno.EOPNOTSUPP, "")):  # a reflink needs no flush
            self.job([f], move=True, verify=False)
        self.assertTrue(flush.called)
        self.assertFalse(f.exists())

    def test_only_verifying_and_moving_wait_for_the_disk_while_copying(self):
        f = self.write("big", os.urandom(5 << 20))
        for kw in ({"verify": False}, {"verify": True}):
            events = []
            with mock.patch.object(core.os, "fdatasync", side_effect=lambda fd: events.append("sync")), mock.patch.object(core.os, "fsync"), \
                    mock.patch.object(core.Job, "_written", lambda job, n: events.append(n)):
                self.job([f], policy="overwrite", **kw)
            self.assertEqual(sum(e for e in events if e != "sync"), 5 << 20, kw)
            self.assertEqual(events.count("sync"), 2 if kw["verify"] else 0, kw)  # a speed first copy never waits while it runs

    def test_every_copy_is_flushed_once_at_the_end_and_a_disk_error_fails_it(self):
        f = self.write("big", os.urandom(2 << 20))
        with mock.patch.object(core.os, "fsync") as fsync:
            self.assertEqual(self.job([f], verify=False, policy="overwrite").results[0].status, "ok")
            fsync.assert_called_once()
        with mock.patch.object(core.os, "fsync", side_effect=OSError(errno.EIO, "disk error")):
            self.assertEqual(self.job([f], verify=False, policy="overwrite").results[0].status, "failed")
        with mock.patch.object(core.os, "fsync", side_effect=OSError(errno.EINVAL, "no flush")):
            self.assertEqual(self.job([f], verify=False, policy="overwrite").results[0].status, "ok")  # a target that cannot flush still copies
        with mock.patch.object(core.os, "fdatasync", side_effect=OSError(errno.EINVAL, "no flush")):
            self.assertEqual(self.job([f], verify=True, policy="overwrite").results[0].status, "failed")  # a flush that verifying needs

    def test_verifying_and_moving_flush_a_file_once_not_twice(self):
        for kw in ({"verify": True}, {"verify": False, "move": True}):
            with self.subTest(kw=kw), CrossDevice(), mock.patch.object(core.os, "fdatasync") as data, mock.patch.object(core, "_sync_dir"), \
                    mock.patch.object(core.os, "fsync") as whole, mock.patch.object(core.fcntl, "ioctl", side_effect=OSError(errno.EOPNOTSUPP, "")):
                self.job([self.write("small", b"x" * 1000)], policy="overwrite", **kw)
            self.assertEqual((data.call_count, whole.call_count), (1, 0), kw)

    def test_a_write_error_while_flushing_fails_the_file(self):
        f = self.write("big", os.urandom(2 << 20))
        with mock.patch.object(core.os, "fdatasync", side_effect=OSError(errno.EIO, "disk error")):
            self.assertEqual(self.job([f], verify=True, policy="overwrite").results[0].status, "failed")

    def test_a_link_on_a_filesystem_without_links_says_so(self):
        link = self.src / "l"
        link.symlink_to("nowhere")
        for code in (errno.EPERM, errno.EOPNOTSUPP):
            with self.subTest(code=code), mock.patch.object(core.os, "symlink", side_effect=OSError(code, "x")):
                job = self.job([link], policy="overwrite")
                self.assertEqual([(r.status, r.error) for r in job.results], [("failed", "The target file system cannot store links")])
        with mock.patch.object(core.os, "symlink", side_effect=OSError(errno.EACCES, "Permission denied")):
            self.assertEqual(self.job([link], policy="overwrite").results[0].error, "Permission denied")  # any other reason stays as the system says it

    def test_a_move_puts_the_new_name_on_the_disk_before_the_source_goes(self):
        events, real_fsync, real_unlink = [], os.fsync, os.unlink
        dst_dir = os.path.realpath(self.dst)

        def unlink(path, *a, **kw):
            events.append(("unlink", str(path)))
            return real_unlink(path, *a, **kw)

        def fsync(fd):  # a folder may refuse with `code`, a file never here
            events.append(os.readlink(f"/proc/self/fd/{fd}"))
            if code and os.path.isdir(f"/proc/self/fd/{fd}"):
                raise OSError(code, "no")
            return real_fsync(fd)

        for verify, code, gone in ((True, 0, True), (False, 0, True), (True, errno.EINVAL, True), (True, errno.EIO, False)):
            with self.subTest(verify=verify, code=code):
                f = self.write("f.txt", b"payload")
                events.clear()
                with CrossDevice(), mock.patch.object(core.os, "fsync", fsync), mock.patch.object(core.os, "unlink", unlink), \
                        mock.patch.object(core.fcntl, "ioctl", side_effect=OSError(errno.EOPNOTSUPP, "")):  # a reflink would skip the flushes
                    job = self.job([f], move=True, verify=verify, policy="overwrite")
                self.assertEqual(f.exists(), not gone)  # EINVAL: a folder that cannot be flushed; EIO: a disk error keeps the source
                self.assertEqual(job.results[0].status, ("verified" if verify else "ok") if gone else "failed")
                self.assertIn(dst_dir, events)
                if gone:
                    self.assertLess(events.index(dst_dir), events.index(("unlink", str(f))), events)

    def test_a_move_keeps_a_source_that_was_saved_after_it_was_read(self):
        class Saved(core.Job):
            def _write(self, src, part, size, verify, tick):
                digest = super()._write(src, part, size, verify, tick)
                Path(src).write_bytes(b"version 2, saved by an editor meanwhile")
                return digest

        for verify in (True, False):  # checked in phase 2, maybe hours later; unchecked right after the copy
            with self.subTest(verify=verify):
                f = self.write("doc.txt", b"version 1")
                with CrossDevice():
                    job = self.job([f], job_class=Saved, move=True, verify=verify, policy="overwrite")
                self.assertEqual([(r.status, r.error) for r in job.results],
                                 [("failed", "Copied, but the source was kept: it was changed after it was read")])
                self.assertEqual(f.read_bytes(), b"version 2, saved by an editor meanwhile")
                self.assertEqual((self.dst / "doc.txt").read_bytes(), b"version 1")  # the copy of what was read is in place
                self.assertClean()

    def test_a_move_keeps_a_source_whose_mtime_was_set_back(self):
        class Restored(core.Job):  # rsync -t, cp -p, touch -r: same size, same mtime, other content
            def _write(self, src, part, size, verify, tick):
                digest = super()._write(src, part, size, verify, tick)
                st = os.stat(src)
                Path(src).write_bytes(b"v2")
                os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns))
                return digest

        f = self.write("doc.txt", b"v1")
        with CrossDevice():
            job = self.job([f], job_class=Restored, move=True)
        self.assertEqual((job.results[0].status, f.read_bytes()), ("failed", b"v2"))

    def test_a_move_puts_new_folders_on_the_disk_before_a_source_goes(self):
        events, real = [], core._sync_dir
        self.write("t/sub/a.txt", b"a")
        with CrossDevice(), mock.patch.object(core, "_sync_dir", lambda p: (events.append(os.path.relpath(p, self.dst)), real(p))):
            job = self.job([self.src / "t"], move=True)
        self.assertEqual(job.results[0].status, "verified")
        self.assertEqual((sorted(events[:2]), events[2:]), ([".", "t"], ["t/sub"]))  # the parents of t and t/sub first, then the copy's own folder

    def test_an_overwrite_never_hits_a_file_that_is_still_to_be_copied(self):
        mine = self.write("b.txt", b"mine")
        theirs = self.write("b.txt", b"theirs", base=self.dst)  # a source of this job, lying where `mine` is going
        self.job([mine, theirs], policy="overwrite")
        self.assertEqual(theirs.read_bytes(), b"theirs")
        self.assertEqual(sorted(p.read_bytes() for p in self.dst.iterdir()), [b"mine", b"theirs", b"theirs"])  # nothing lost, each under its own name

    def test_skipped_files_keep_their_folder(self):
        self.write("d/keep.txt", b"new")
        os.utime(self.write("d/keep.txt", b"old", base=self.dst), (1, 1))
        self.job([self.src / "d"], move=True, policy="skip")
        self.assertTrue((self.src / "d/keep.txt").exists())


class CrossDevice:
    """Makes the plain rename of a move fail like it does between two disks."""
    def __enter__(self):
        real = os.replace

        def replace(a, b):
            if str(a).endswith(core.PART):
                return real(a, b)
            raise OSError(errno.EXDEV, "cross-device")

        self.patch = mock.patch.object(core.os, "replace", replace)
        return self.patch.__enter__()

    def __exit__(self, *exc):
        return self.patch.__exit__(*exc)


class TerminalTests(Base):
    """The terminal mode (cli.py): stdout is no terminal here, so no bar is drawn and the text is plain."""

    def run_cli(self, *argv):
        out = io.StringIO()
        with mock.patch.object(desktop, "load_config", lambda: dict(desktop.DEFAULTS)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            return cli.run(cli.parser.parse_args(argv)), out.getvalue()

    def test_copy_is_reported_and_exits_zero(self):
        a = self.write("a.txt", b"alpha")
        code, text = self.run_cli("copy", "--to", str(self.dst), str(a))
        self.assertEqual(code, cli.OK)
        self.assertEqual((self.dst / "a.txt").read_bytes(), b"alpha")
        self.assertIn("a.txt", text)
        self.assertIn(cli.TICK, text)

    def test_conflict_policy_from_the_command_line(self):
        a = self.write("a.txt", b"new")
        self.write("a.txt", b"old", self.dst)
        code, text = self.run_cli("copy", "--to", str(self.dst), "--on-conflict", "skip", str(a))
        self.assertEqual((code, (self.dst / "a.txt").read_bytes()), (cli.OK, b"old"))
        self.assertIn(cli.WARN, text)
        self.run_cli("copy", "--to", str(self.dst), "--on-conflict", "overwrite", str(a))
        self.assertEqual((self.dst / "a.txt").read_bytes(), b"new")

    def test_a_failure_is_exit_one_and_a_bad_call_is_two(self):
        code, text = self.run_cli("copy", "--to", str(self.dst), str(self.src / "missing"))
        self.assertEqual(code, cli.FAILED)
        self.assertIn(cli.CROSS, text)
        file = self.write("plain", b"x")
        self.assertEqual(self.run_cli("copy", "--to", str(file), str(file))[0], cli.USAGE)

    def test_quiet_leaves_out_the_files_that_are_fine(self):
        a = self.write("a.txt", b"alpha")
        code, text = self.run_cli("copy", "-q", "--to", str(self.dst), str(a))
        self.assertEqual(code, cli.OK)
        self.assertNotIn("a.txt", text)


class SameNameTests(Base):
    """Two sources with one name (search results, clipboard) must keep both, however the workers interleave."""

    def pairs(self, n):
        return [self.write(f"{side}/f{i}.txt", f"{side}{i}".encode()) for i in range(n) for side in "AB"]

    def contents(self):
        return sorted(p.read_bytes().decode() for p in self.dst.iterdir())

    def test_copy_keeps_both(self):
        for workers in (1, 4):
            with self.subTest(workers=workers):
                for p in self.dst.iterdir():
                    p.unlink()
                self.job(self.pairs(30), workers=workers, verify=False)
                self.assertEqual(self.contents(), sorted(f"{side}{i}" for i in range(30) for side in "AB"))
                self.assertClean()

    def test_move_keeps_both(self):
        for workers in (1, 4):
            with self.subTest(workers=workers):
                for p in self.dst.iterdir():
                    p.unlink()
                files = self.pairs(30)
                self.job(files, move=True, workers=workers)
                self.assertEqual(len(self.contents()), 60)
                self.assertEqual([p for p in files if p.exists()], [])

    def test_overwrite_policy_never_overwrites_a_file_of_the_same_job(self):
        self.write("x.bin", b"old", base=self.dst)
        self.job([self.write("A/x.bin", b"A"), self.write("B/x.bin", b"B")], policy="overwrite", workers=1)
        self.assertEqual(self.contents(), ["A", "B"])  # the old file made way for one of them, the other was kept beside it


class CaseFoldingTargetTests(Base):
    def test_names_that_differ_only_in_case_never_share_a_target(self):
        # exFAT, NTFS and SMB targets treat them as one file: the second must get a new name, however the workers interleave
        job = core.Job([])
        first = job._resolve("/x/Readme.md", str(self.dst / "Readme.md"))
        second = job._resolve("/x/README.md", str(self.dst / "README.md"))
        self.assertEqual((first, second), (str(self.dst / "Readme.md"), str(self.dst / "README (2).md")))
        self.assertEqual(job._resolve("/x/readme.MD", str(self.dst / "readme.MD")), str(self.dst / "readme (3).MD"))


class DamagedPlanTests(Base):
    def test_a_missing_source_fails_alone(self):
        job = self.job([self.src / "gone", self.write("ok.txt", b"ok")])
        self.assertEqual((job.fatal, self.status(job)), ("", {"gone": "failed", "ok.txt": "verified"}))
        self.assertEqual((job.count, len(job.results)), (1, 2))  # "items remaining" ignores the refusal

    def test_a_folder_that_cannot_be_created_fails_without_its_content(self):
        self.write("d/sub/a.txt", b"a")
        self.write("d", b"a file in the way", base=self.dst)
        job = self.job([self.src / "d", self.write("ok.txt", b"ok")])
        self.assertEqual((job.fatal, self.status(job)), ("", {"d": "failed", "ok.txt": "verified"}))  # one line, not one per subfolder

    def test_a_source_that_is_gone_while_the_target_exists_fails_alone(self):
        self.write("gone.txt", b"already moved", base=self.dst)  # a second paste after cut + paste into the same folder
        job = self.job([self.src / "gone.txt", self.write("ok.txt", b"ok")], move=True)
        self.assertEqual((job.fatal, self.status(job)), ("", {"gone.txt": "failed", "ok.txt": "ok"}))


class PlantedPartTest(Base):
    def test_a_planted_part_link_is_replaced_not_written_through(self):
        victim = self.write("victim.txt", b"precious", base=self.dst.parent)
        self.write("a.txt", b"new")
        os.symlink(victim, str(self.dst / "a.txt") + core.PART)
        job = self.job([self.src / "a.txt"])
        self.assertEqual(victim.read_bytes(), b"precious")
        self.assertEqual((self.dst / "a.txt").read_bytes(), b"new")
        self.assertEqual(self.status(job), {"a.txt": "verified"})
        self.assertClean()


class RetryMoveTest(Base):
    def test_a_retried_move_removes_the_folders_the_first_run_left(self):
        self.write("album/cd1/a.flac", b"a")
        b = self.write("album/cd1/b.flac", b"b")
        real_open = open

        def flaky(path, *args, **kw):
            if path == str(b):
                raise PermissionError(errno.EACCES, "denied")
            return real_open(path, *args, **kw)

        with CrossDevice(), mock.patch("builtins.open", flaky):  # a copy and delete, as between two disks
            first = self.job([self.src / "album"], move=True)
        self.assertEqual(self.status(first)["b.flac"], "failed")
        self.assertTrue(b.exists())
        retry = core.Job([(r.src, r.dst) for r in first.results if r.status == "failed"], move=True, dirs=first.dirs)
        with CrossDevice():
            retry.run()
        self.assertEqual((self.dst / "album/cd1/b.flac").read_bytes(), b"b")
        self.assertFalse((self.src / "album").exists())


class LenientTargetTest(Base):
    def test_names_a_lenient_filesystem_takes_for_one_get_two_targets(self):
        self.assertEqual(core.name_key("/t/Caf\u00e9./a"), core.name_key("/t/cafe\u0301/A"))  # case, a trailing dot, NFC against NFD
        self.assertNotEqual(core.name_key("/t/.hidden"), core.name_key("/t/hidden"))  # only a dot at the end goes
        names = ["Notizen", "Notizen.", "Caf\u00e9.flac", "Cafe\u0301.flac"]
        for n in names:
            self.write(n, n.encode())
        job = core.Job(core.items_for([str(self.src / n) for n in names], str(self.dst)), verify=False, workers=1)
        keys = {core.name_key(job._resolve(a, b)) for a, b in job.items}
        self.assertEqual(len(keys), 4)


class LeftoverPartTest(Base):
    def test_a_leftover_part_is_skipped_not_taken_for_the_temp_file_of_its_neighbour(self):
        big = os.urandom(3 << 20)
        self.write("d/big.bin", big)
        self.write("d/big.bin" + core.PART, b"leftover")
        os.symlink("big.bin", self.src / "d/big.bin.TRUSTPART")  # a link, and the case exFAT ignores
        for move in (False, True):
            with CrossDevice() if move else contextlib.nullcontext():
                job = self.job([self.src / "d"], move=move, verify=False, workers=4)
            self.assertEqual((self.dst / "d/big.bin").read_bytes(), big)
            self.assertEqual((self.status(job)["big.bin" + core.PART], self.status(job)["big.bin.TRUSTPART"]), ("skipped", "skipped"))
            self.assertFalse((self.dst / ("d/big.bin" + core.PART)).exists())
        self.assertTrue((self.src / ("d/big.bin" + core.PART)).exists())  # the move leaves it, and so its folder


class LinkToFolderTest(Base):
    def test_a_link_to_a_folder_can_be_copied_into_that_folder(self):
        self.write("Photos/a.jpg", b"x")
        os.symlink("Photos", self.src / "Photos-link")
        job = core.Job(core.items_for([str(self.src / "Photos-link")], str(self.src / "Photos")))
        job.run()
        self.assertEqual(os.readlink(self.src / "Photos/Photos-link"), "Photos")


class CancelTest(Base):
    def test_cancel_leaves_nothing_behind(self):
        started, go = threading.Event(), threading.Event()

        class Slow(core.Job):
            def _pump(self, *a):
                started.set()
                go.wait(5)
                super()._pump(*a)

        f = self.write("a/big.bin", os.urandom(6 << 20))
        job = Slow(core.items_for([str(self.src / "a")], str(self.dst)))
        t = threading.Thread(target=job.run)
        t.start()
        self.assertTrue(started.wait(5))
        self.assertEqual(len(list(self.dst.rglob("*" + core.PART))), 1)  # temp file exists mid-copy
        job.cancel()
        go.set()
        t.join(5)
        self.assertTrue(job.finished)
        self.assertEqual(list(self.dst.rglob("*")), [])  # no part file, no half file, no empty folders
        self.assertTrue(f.exists())


class ClipboardTests(unittest.TestCase):
    def test_cut_and_copy(self):
        self.assertEqual(core.parse_clipboard("cut\nfile:///a%20b\nfile:///c"), ("move", ["file:///a%20b", "file:///c"]))
        self.assertEqual(core.parse_clipboard("copy\nfile:///a\n"), ("copy", ["file:///a"]))

    def test_plain_uri_list(self):
        self.assertEqual(core.parse_clipboard("# c\r\nfile:///a\r\nfile:///b\r\n"), ("copy", ["file:///a", "file:///b"]))




class TwoPhases(Base):
    """Every file is written first, then every copy is read back; only a checked copy gets its name."""

    def make(self, *names):
        return [self.write(n, n.encode() * 3000) for n in names]

    def test_all_files_are_written_before_the_first_check_and_named_after_it(self):
        events = []

        class Watch(core.Job):
            def _write(self, src, part, size, verify, tick):
                events.append("write " + Path(src).name)
                return super()._write(src, part, size, verify, tick)

            def _hash(self, path, tick, size=None):
                events.append(("check", Path(path).name, (self.dst_dir / "a").exists()))
                return super()._hash(path, tick, size)

        Watch.dst_dir = self.dst
        job = self.job(self.make("a", "b"), job_class=Watch, workers=1)
        self.assertEqual(events[:2], ["write a", "write b"])
        self.assertEqual([e[1:] for e in events[2:]], [("a" + core.PART, False), ("b" + core.PART, True)])  # "a" gets its name after its own check
        self.assertEqual(self.status(job), {"a": "verified", "b": "verified"})
        self.assertEqual(job.done, job.total)
        self.assertClean()

    def test_a_move_removes_each_source_after_its_own_check(self):
        seen = []

        class Watch(core.Job):
            def _hash(self, path, tick, size=None):
                seen.append((Path(path).name, [p.exists() for p in (a, b)]))
                return super()._hash(path, tick, size)

        a, b = self.make("a", "b")
        with CrossDevice():
            job = self.job([a, b], job_class=Watch, move=True, workers=1)
        self.assertEqual(seen, [("a" + core.PART, [True, True]), ("b" + core.PART, [False, True])])
        self.assertEqual(self.status(job), {"a": "verified", "b": "verified"})
        self.assertFalse(a.exists() or b.exists())

    def test_a_bad_copy_in_a_move_keeps_its_source_and_gets_no_name(self):
        Corrupting.bad = 99
        (a,) = self.make("a")
        with CrossDevice():
            job = self.job([a], job_class=Corrupting, move=True)
        self.assertEqual(self.status(job), {"a": "failed"})
        self.assertTrue(a.exists())
        self.assertEqual(list(self.dst.iterdir()), [])

    def test_a_cancel_between_the_phases_leaves_the_sources_and_no_trace(self):
        class Cancel(core.Job):
            def _hash(self, path, tick, size=None):
                self.cancel()
                return super()._hash(path, tick, size)

        files = self.make("a", "b")
        with CrossDevice():
            job = self.job(files, job_class=Cancel, move=True, workers=1)
        self.assertEqual((job.results, list(self.dst.iterdir())), ([], []))  # the unchecked copies are neither results nor files
        self.assertTrue(all(f.exists() for f in files))

    def test_a_source_that_is_in_a_move_twice_goes_only_after_a_check_and_fails_nowhere(self):
        for _run in range(40):  # search results: a folder and a file inside it, both selected; the workers interleave differently each time
            early = []

            class Watch(core.Job):
                def _release(self, src, dst):
                    early.append(not self.check_pass)
                    return super()._release(src, dst)

            a = self.write("folder/a", b"a" * 3000)
            with CrossDevice():
                job = self.job([a.parent, a], job_class=Watch, move=True, workers=4)
            self.assertEqual(sorted(r.status for r in job.results), ["verified", "verified"])
            self.assertEqual(early, [False, False])  # never while the copies still wait for their check
            self.assertEqual((a.exists(), (self.dst / "a").read_bytes(), (self.dst / "folder/a").read_bytes()), (False, b"a" * 3000, b"a" * 3000))
            self.assertClean()
            shutil.rmtree(self.dst / "folder")
            (self.dst / "a").unlink()

    def test_step_2_does_not_search_the_waiting_copies_after_every_file(self):
        class Watch(core.Job):
            def _check_one(self, result, digest, part):
                waiting.append(len(self._to_check))
                return super()._check_one(result, digest, part)

        waiting = []
        job = self.job(self.make("a", "b", "c"), job_class=Watch, workers=1)
        self.assertEqual((waiting, job._to_check, job._checks), ([3, 3, 3], [], 0))  # the list is left alone while the checks run, and empty at the end
        self.assertEqual(set(self.status(job).values()), {"verified"})
        self.assertClean()

    def test_switching_verification_off_leaves_nothing_to_jump_at_the_end_of_the_copy(self):
        ratio = []

        class Flip(core.Job):
            def _transfer(self, *args):
                out = super()._transfer(*args)
                self.verify = False  # off after the first file, which is waiting for its check
                return out

            def _check_one(self, *args):
                ratio.append(self.done / self.total)
                super()._check_one(*args)

        job = self.job(self.make("a", "b"), job_class=Flip, workers=1)
        self.assertEqual(ratio[0], 1.0)  # the copy is all there is to do: the bar was full before the checks were skipped
        self.assertEqual(self.status(job), {"a": "ok", "b": "ok"})
        self.assertEqual(job.readback, 0)
        self.assertEqual({p.name for p in self.dst.iterdir()}, {"a", "b"})  # the skipped check still names the copy


class LiveVerify(Base):
    """Verification switched on or off while the job runs."""

    def run_flipping(self, start, flip_to, corrupt_first=False):
        class Flip(core.Job):
            def _write(self, src, part, size, verify, tick):
                digest = super()._write(src, part, size, verify, tick)
                if src.endswith("a"):  # the first file is done being written: switch now
                    self.verify = flip_to
                return digest

        for name in "ab":
            self.write(name, name.encode() * 5000)
        job = Flip(core.items_for([str(self.src / "a"), str(self.src / "b")], str(self.dst)), verify=start, workers=1)
        if corrupt_first:
            real = job._verify_late

            def damage_then_verify(r):
                (self.dst / "a").write_bytes(b"x")
                real(r)

            job._verify_late = damage_then_verify
        job.run()
        return job

    def test_retry_replaces_the_bad_copy_the_late_check_left(self):
        for policy in ("skip", "newer", "keep_both"):
            with self.subTest(policy=policy):
                job = self.run_flipping(start=False, flip_to=True, corrupt_first=True)
                failed = [r for r in job.results if r.status == "failed"]
                self.assertEqual([r.kept for r in failed], [True])
                again = core.Job([(r.src, r.dst) for r in failed], verify=True, policy=policy, workers=1, replace={r.dst for r in job.results if r.kept})
                again.run()
                self.assertEqual([r.status for r in again.results], ["verified"])
                self.assertEqual((self.dst / "a").read_bytes(), b"a" * 5000)
                self.assertEqual(sorted(p.name for p in self.dst.iterdir()), ["a", "b"])  # no "(2)" beside it
                for p in self.dst.iterdir():
                    p.unlink()

    def test_switching_on_checks_the_earlier_files_too(self):
        job = self.run_flipping(start=False, flip_to=True)
        self.assertEqual({r.status for r in job.results}, {"verified"})
        self.assertTrue(all(r.digest for r in job.results))
        self.assertEqual(job.done, job.total)

    def test_the_bar_and_the_time_left_do_not_jump_when_verification_is_switched_off_in_a_file(self):
        samples = []

        class Flip(core.Job):
            def _write(self, src, part, size, verify, tick):
                def ticking(n, *skip):
                    tick(n, *skip)
                    if len(samples) == 2:
                        self.verify = False  # while this file is written: its read-back was promised and is dropped now
                    samples.append((self.step_fraction, self.step_left, self.moved))
                return super()._write(src, part, size, verify, ticking)

        self.write("a.bin", os.urandom(12 << 20))
        job = Flip(core.items_for([str(self.src / "a.bin")], str(self.dst)), verify=True, workers=1)
        with mock.patch.object(core, "SYNC_MAX", core.SYNC_MIN):  # a flush, and with it a tick, every MiB: a fast disk would report the file in two
            job.run()
        self.assertGreater(len(samples), 4)
        fractions = [f for f, _left, _moved in samples]
        self.assertEqual(fractions, sorted(fractions))  # never back: the switch must not halve the bar
        lefts = [left for _f, left, _moved in samples]
        self.assertEqual(lefts, sorted(lefts, reverse=True))  # the time left only falls
        self.assertEqual(job.moved, 12 << 20)  # a dropped read-back moved no byte: the speed never sees it

    def test_switched_off_before_step_2_there_is_no_step_2(self):
        class Flip(core.Job):
            def _write(self, src, part, size, verify, tick):
                digest = super()._write(src, part, size, verify, tick)
                if src.endswith("b"):  # the first file was written with verification and waits for its read-back: now it is switched off
                    self.verify = False
                return digest

        for name in "ab":
            self.write(name, name.encode() * 5000)
        job = Flip(core.items_for([str(self.src / "a"), str(self.src / "b")], str(self.dst)), verify=True, workers=1)
        job.run()
        self.assertFalse(job.check_pass)
        self.assertEqual({r.status for r in job.results}, {"ok"})  # copied, not verified, and in place
        self.assertEqual((self.dst / "a").read_bytes(), b"a" * 5000)

    def test_each_file_has_its_own_phase(self):
        seen = {}

        class Watch(core.Job):
            def _write(self, src, part, size, verify, tick):
                seen["write"] = dict(self.phases)
                return super()._write(src, part, size, verify, tick)

            def _hash(self, path, tick, size=None):
                seen["hash"] = dict(self.phases)
                return super()._hash(path, tick, size)

        self.write("a", b"a" * 100)
        job = Watch(core.items_for([str(self.src / "a")], str(self.dst)), verify=True, workers=1)
        self.assertEqual(job.phase, "scan")
        job.run()
        self.assertEqual(seen, {"write": {str(self.src / "a"): "copy"}, "hash": {str(self.src / "a"): "verify"}})
        self.assertEqual(job.phases, {})  # nothing in flight any more

    def test_a_file_still_being_written_decides_the_heading(self):
        job = core.Job([])
        job.stage = "copy"
        job.phases = {"big.iso": "copy", "small": "verify"}  # a small file verified while the big one is still written
        self.assertEqual(job.phase, "copy")
        job.phases = {"small": "verify"}
        self.assertEqual(job.phase, "verify")

    def test_bytes_are_counted_block_by_block(self):
        self.write("a", b"a" * 1000)
        job = core.Job(core.items_for([str(self.src / "a")], str(self.dst)), verify=True, workers=1)
        job.run()
        self.assertEqual((Path(job.current).name, job.written, job.readback), ("a", 1000, 1000))  # so the speed is right mid-file

    def test_files_are_planned_a_to_z(self):
        for name in ("b", "C", "a"):
            self.write(name, b"x")
        job = core.Job(core.items_for([str(self.src / n) for n in ("b", "C", "a")], str(self.dst)))
        job.run()
        self.assertEqual([Path(f[0]).name for f in job.files], ["a", "b", "C"])

    def test_late_check_finds_a_bad_copy(self):
        job = self.run_flipping(start=False, flip_to=True, corrupt_first=True)
        self.assertEqual({Path(r.src).name: r.status for r in job.results}, {"a": "failed", "b": "verified"})

    def test_switching_off_stops_a_readback_that_is_already_running(self):
        class Flip(core.Job):
            def _hash(self, path, tick, size=None):
                def off_after_a_chunk(n):
                    tick(n)
                    self.verify = False
                return super()._hash(path, off_after_a_chunk, size)

        self.write("a", b"a" * 5000)
        with mock.patch.object(core, "CHUNK", 1000):
            job = Flip(core.items_for([str(self.src / "a")], str(self.dst)), verify=True, workers=1)
            job.run()
        self.assertEqual([(r.status, r.digest) for r in job.results], [("ok", "")])  # copied, not checked
        self.assertEqual(job.readback, 1000)  # one chunk read, then it stopped
        self.assertEqual(job.done, job.total)  # the unread rest counts as done
        self.assertEqual((self.dst / "a").read_bytes(), b"a" * 5000)

    def test_switching_off_skips_the_readback_of_the_running_file_too(self):
        job = self.run_flipping(start=True, flip_to=False)
        self.assertEqual({Path(r.src).name: r.status for r in job.results}, {"a": "ok", "b": "ok"})
        self.assertEqual(job.readback, 0)
        self.assertEqual(job.done, job.total)


class Settings(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        for name, path in (("CONFIG_FILE", self.tmp / "x" / "settings.json"), ("SCRIPTS_DIR", self.tmp / "scripts"),
                           ("FORKS", {"nemo": self.tmp / "nemo", "caja": self.tmp / "caja"})):  # never the real folders
            patcher = mock.patch.object(desktop, name, path)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(i18n.set_language, "en")

    def test_reset_returns_to_the_defaults(self):
        desktop.set_option("algo", "sha256")
        desktop.set_option("verify", False)
        self.assertEqual(desktop.load_config()["algo"], "sha256")
        self.assertIsNone(desktop.reset_config())
        self.assertEqual(desktop.load_config(), desktop.DEFAULTS)
        self.assertIsNone(desktop.reset_config())  # nothing saved: still fine

    def test_reset_also_resets_the_language_and_the_entries(self):
        german_system = {"LANGUAGE": "", "LC_ALL": "", "LC_MESSAGES": "", "LANG": "de_DE.UTF-8"}  # whatever this machine speaks
        with mock.patch.dict(os.environ, german_system):
            desktop.set_option("language", "en")
            desktop.set_option("menu", "folder")
            self.assertIsNone(desktop.reset_config())
            self.assertEqual(i18n._current, "de")
            self.assertEqual(sorted(p.name for p in desktop.SCRIPTS_DIR.iterdir()), sorted(p.name for p in desktop.menu_entries()))
            self.assertEqual(desktop.menu_entries()[0].name, "TrustCopy: Kopieren nach …")

    def test_reset_without_a_saved_file_creates_nothing(self):
        self.assertIsNone(desktop.reset_config())  # not even the settings folder exists yet
        self.assertFalse(desktop.CONFIG_FILE.parent.exists())

    def test_reset_keeps_a_settings_file_that_is_a_link(self):
        real = self.tmp / "dotfiles" / "settings.json"
        real.parent.mkdir()
        real.write_text('{"algo": "sha256"}')
        desktop.CONFIG_FILE.parent.mkdir()
        desktop.CONFIG_FILE.symlink_to(real)
        self.assertIsNone(desktop.reset_config())
        self.assertTrue(desktop.CONFIG_FILE.is_symlink())
        self.assertEqual(desktop.load_config(), desktop.DEFAULTS)
        self.assertEqual(json.loads(real.read_text()), {})  # the file behind the link was emptied, not replaced

    def test_every_algorithm_hashes_like_its_standard(self):
        for algo, digest in (("blake2b", "ba80a53f981c4d0d6a2797b69f12f6e94c212f14685ac4b74b12bb6fdbffa2d17d87c5392aab792dc252d5de4533cc9518d38aa8dbf1925ab92386edd4009923"), ("sha256", "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")):
            h = core.ALGOS[algo]()
            h.update(b"abc")
            self.assertEqual(h.hexdigest(), digest)

    def test_only_deviations_from_the_defaults_are_saved(self):
        desktop.set_option("algo", "sha256")
        desktop.set_option("verify", True)  # equals its default
        self.assertEqual(json.loads(desktop.CONFIG_FILE.read_text()), {"algo": "sha256"})
        desktop.set_option("algo", desktop.DEFAULTS["algo"])  # back to the default: nothing left to remember
        self.assertEqual(json.loads(desktop.CONFIG_FILE.read_text()), {})

    def test_invalid_saved_values_fall_back_to_the_defaults(self):
        desktop.CONFIG_FILE.parent.mkdir()
        for bad in ({"algo": "md5"}, {"after": "never"}, {"conflict": 3}, {"menu": "tree"}, {"verify": "yes"},
                    {"language": "xx"}, {"details": 1}, {"columns": "name"}, [1, 2], "text", None):
            desktop.CONFIG_FILE.write_text(json.dumps(bad))
            self.assertEqual(desktop.load_config(), desktop.DEFAULTS, bad)
        desktop.CONFIG_FILE.write_text(json.dumps({"algo": "sha256", "after": "never", "unknown": 1}))
        self.assertEqual(desktop.load_config(), {**desktop.DEFAULTS, "algo": "sha256"})  # the valid part is kept

    def test_names_that_are_no_utf8_neither_stop_a_save_nor_a_label(self):
        self.assertEqual(desktop.shown(os.fsdecode(b"M\xe4rz.txt")), "M\ufffdrz.txt")
        self.assertEqual(desktop.shown("März"), "März")
        with tempfile.TemporaryDirectory() as tmp:
            desktop.write_atomic(os.path.join(tmp, "x"), os.fsdecode(b"M\xe4rz") + "\n")
            self.assertEqual(sorted(os.listdir(tmp)), ["x"])  # written, and no .part left

    def test_a_planted_part_link_is_not_written_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            victim = Path(tmp, "victim")
            victim.write_text("precious\n")
            os.symlink(victim, Path(tmp, "x.part"))
            desktop.write_atomic(os.path.join(tmp, "x"), "new\n")
            self.assertEqual((victim.read_text(), Path(tmp, "x").read_text()), ("precious\n", "new\n"))
            self.assertFalse(Path(tmp, "x").is_symlink())

    def test_desktop_starters_quote_the_launcher(self):
        for template in (SRC.parent / "data").glob("*.desktop.in"):
            lines = [l for l in template.read_text().splitlines() if l.startswith("Exec=")]
            self.assertTrue(lines and all(l.startswith('Exec="@LAUNCHER@"') for l in lines), template.name)  # a home path may hold a space

    def test_saving_is_atomic_keeps_mode_and_links(self):
        desktop.set_option("algo", "sha256")
        desktop.CONFIG_FILE.chmod(0o600)
        desktop.set_option("verify", False)
        self.assertEqual(stat.S_IMODE(desktop.CONFIG_FILE.stat().st_mode), 0o600)
        self.assertEqual(os.listdir(desktop.CONFIG_FILE.parent), ["settings.json"])  # no .part left behind
        real = self.tmp / "dotfiles" / "settings.json"
        real.parent.mkdir()
        desktop.CONFIG_FILE.replace(real)
        desktop.CONFIG_FILE.symlink_to(real)
        desktop.set_option("algo", "blake2b")
        self.assertTrue(desktop.CONFIG_FILE.is_symlink())
        self.assertEqual(json.loads(real.read_text())["algo"], "blake2b")

    def test_a_save_is_on_the_disk_before_it_gets_its_name(self):
        events, real = [], os.replace
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(desktop.os, "fsync", side_effect=lambda fd: events.append("flush")), \
                mock.patch.object(desktop.os, "replace", side_effect=lambda a, b: (events.append("name"), real(a, b))):
            desktop.write_atomic(os.path.join(tmp, "x"), "new\n")
        self.assertEqual(events, ["flush", "name"])  # a power cut in between leaves the old file, never an empty one

    def test_a_failing_save_keeps_the_old_file_and_leaves_no_part_behind(self):
        desktop.set_option("algo", "sha256")
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            self.assertIn("disk full", desktop.set_option("algo", "blake2b"))
        self.assertEqual(json.loads(desktop.CONFIG_FILE.read_text()), {"algo": "sha256"})
        self.assertEqual(os.listdir(desktop.CONFIG_FILE.parent), ["settings.json"])

    @unittest.skipIf(os.geteuid() == 0, "root can write anywhere")
    def test_an_unwritable_folder_gives_an_error_text(self):
        desktop.CONFIG_FILE.parent.mkdir()
        desktop.CONFIG_FILE.parent.chmod(0o500)
        self.addCleanup(desktop.CONFIG_FILE.parent.chmod, 0o700)
        self.assertIsInstance(desktop.set_option("algo", "sha256"), str)

    def test_language_follows_gettext_with_a_fallback_to_the_locale(self):
        def detect(**env):
            with mock.patch.dict(os.environ, {"LANGUAGE": "", "LC_ALL": "", "LC_MESSAGES": "", "LANG": "", **env}):
                return i18n.detect()
        self.assertEqual(detect(LANGUAGE="nds:de", LANG="en_US.UTF-8"), "de")  # every entry of the list
        self.assertEqual(detect(LANGUAGE="xx:yy", LANG="fr_FR.UTF-8"), "fr")  # gettext would give up here, we use the locale
        self.assertEqual(detect(LC_ALL="es_ES.UTF-8", LANG="de_DE.UTF-8"), "es")  # LC_ALL beats LANG
        self.assertEqual(detect(LC_ALL="C", LANG="de_DE.UTF-8"), "en")  # the usual way to force English
        for locale in ("C", "C.UTF-8", "POSIX"):
            self.assertEqual(detect(LANGUAGE="de", LC_ALL=locale), "en", locale)  # LANGUAGE counts for no locale but a real one
        self.assertEqual(detect(LANG="C"), "en")
        self.assertEqual(detect(LANGUAGE="de"), "en")  # no locale at all is "C"
        self.assertEqual(detect(), "en")


class Results(Base):
    def test_results_carry_size_and_checksum(self):
        self.write("a", b"hello")
        job = self.job([self.src / "a"], algo="sha256")
        (r,) = job.results
        self.assertEqual((r.status, r.size), ("verified", 5))
        self.assertEqual(r.digest, hashlib.sha256(b"hello").hexdigest())
        self.assertEqual(job.files[0][3], 5)  # the plan is public for the file list

    def test_no_checksum_without_verification(self):
        self.write("a", b"hello")
        self.assertEqual(self.job([self.src / "a"], verify=False).results[0].digest, "")


class Integration(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, path in (("SCRIPTS_DIR", Path(tmp.name, "scripts")), ("CONFIG_FILE", Path(tmp.name, "settings.json")),
                           ("ACCELS_FILE", Path(tmp.name, "nautilus", "scripts-accels")),
                           ("FORKS", {"nemo": Path(tmp.name, "nemo"), "caja": Path(tmp.name, "caja")})):  # never the real folders
            patcher = mock.patch.object(desktop, name, path)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(i18n.set_language, "en")

    def names(self):
        return sorted(p.name for p in desktop.SCRIPTS_DIR.iterdir())

    def test_scripts_are_named_in_the_current_language_and_replaced_on_change(self):
        i18n.set_language("de")
        self.assertIsNone(desktop.install_scripts())
        self.assertEqual(self.names(), ["TrustCopy: Aus der Zwischenablage einfügen", "TrustCopy: Kopieren nach …", "TrustCopy: Verschieben nach …"])
        i18n.set_language("en")
        desktop.install_scripts()
        self.assertEqual(self.names(), ["TrustCopy: Copy to…", "TrustCopy: Move to…", "TrustCopy: Paste from clipboard"])
        script = desktop.SCRIPTS_DIR / "TrustCopy: Copy to…"
        self.assertTrue(script.stat().st_mode & stat.S_IXUSR)
        self.assertIn(" copy --", script.read_text())
        desktop.install_scripts(remove_only=True)
        self.assertEqual(self.names(), [])

    def test_menu_entries_follow_style_and_language(self):
        i18n.set_language("en")
        flat = desktop.menu_entries()
        desktop.set_option("menu", "folder")
        folder = desktop.menu_entries()
        self.assertEqual([p.parent.name for p in folder], ["TrustCopy"] * 3)
        self.assertNotEqual(flat, folder)
        i18n.set_language("de")
        self.assertNotEqual(desktop.menu_entries(), folder)  # a language change renames them too, so Files has to restart
        i18n.set_language("en")
        desktop.set_option("menu", "scripts")
        self.assertEqual(desktop.menu_entries(), flat)

    def test_submenu_style_and_back(self):
        desktop.set_option("menu", "folder")
        desktop.install_scripts()
        self.assertEqual(self.names(), ["TrustCopy"])
        self.assertEqual(sorted(p.name for p in (desktop.SCRIPTS_DIR / "TrustCopy").iterdir()), ["Copy to…", "Move to…", "Paste from clipboard"])
        desktop.set_option("menu", "scripts")
        desktop.install_scripts()
        self.assertEqual(self.names(), ["TrustCopy: Copy to…", "TrustCopy: Move to…", "TrustCopy: Paste from clipboard"])  # folder is gone
        desktop.install_scripts(remove_only=True)
        self.assertEqual(self.names(), [])

    def test_forks_get_entries_only_when_installed_and_lose_them_always(self):
        nemo, caja = desktop.FORKS["nemo"], desktop.FORKS["caja"]
        caja.mkdir()
        (caja / "TrustCopy: Copy to…").write_text(desktop.script_text("copy"))  # left from when Caja was installed
        (caja / "My own script").write_text("#!/bin/sh\n")
        with mock.patch.object(core.shutil, "which", lambda name: "/usr/bin/nemo" if name == "nemo" else None):
            self.assertIsNone(desktop.install_scripts())
        self.assertEqual(sorted(p.name for p in nemo.iterdir()), self.names())
        for name in self.names():
            self.assertEqual((nemo / name).read_text(), (desktop.SCRIPTS_DIR / name).read_text())  # one text for every manager
        self.assertEqual([p.name for p in caja.iterdir()], ["My own script"])  # not installed: ours went, the user's stays
        desktop.install_scripts(remove_only=True)
        self.assertEqual(list(nemo.iterdir()), [])

    def test_an_entry_reads_the_selection_of_whichever_manager_started_it(self):
        fake = desktop.SCRIPTS_DIR.parent / "fake"
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')  # stands for the launcher: prints what the entry hands it
        fake.chmod(0o755)
        with mock.patch.object(desktop, "LAUNCHER", fake):
            self.assertIsNone(desktop.install_scripts())
        for var in ("NAUTILUS", "NEMO", "CAJA"):
            for title, args in (("Copy to…", ["copy", "--", "file:///a b", "file:///it's $x"]), ("Paste from clipboard", ["paste", "--to", "file:///here"])):
                with self.subTest(var=var, title=title):
                    env = {"PATH": os.environ["PATH"], f"{var}_SCRIPT_SELECTED_URIS": "file:///a b\nfile:///it's $x", f"{var}_SCRIPT_CURRENT_URI": "file:///here"}
                    out = subprocess.run([str(desktop.SCRIPTS_DIR / ("TrustCopy: " + title))], env=env, capture_output=True, text=True).stdout
                    self.assertEqual(out.split("\n")[:-1], args)

    def test_a_forks_folder_linked_to_the_files_one_keeps_its_entries(self):
        desktop.FORKS["nemo"].symlink_to(desktop.SCRIPTS_DIR)  # a common tip: one folder for both managers
        for installed in (False, True):
            with mock.patch.object(core.shutil, "which", lambda name: "/usr/bin/nemo" if installed and name == "nemo" else None):
                self.assertIsNone(desktop.install_scripts())
            self.assertEqual(len(self.names()), 3, installed)

    def test_a_symlink_loop_as_a_scripts_folder_is_an_error_text_not_a_crash(self):
        desktop.FORKS["nemo"].parent.mkdir(parents=True, exist_ok=True)
        desktop.FORKS["nemo"].symlink_to(desktop.FORKS["nemo"])  # points at itself
        with mock.patch.object(core.shutil, "which", lambda name: "/usr/bin/nemo"):  # installed: its folder is needed (Python 3.13 resolves a loop without an error)
            self.assertIsInstance(desktop.install_scripts(), str)

    def accels(self):
        return desktop.ACCELS_FILE.read_text().splitlines() if desktop.ACCELS_FILE.exists() else []

    def test_shortcuts_are_written_renamed_with_the_language_and_removed(self):
        i18n.set_language("en")
        self.assertIsNone(desktop.set_option("shortcuts", True))
        self.assertEqual(self.accels(), [desktop.ACCELS_BEGIN, "<Primary><Alt>c TrustCopy: Copy to…", "<Primary><Alt>x TrustCopy: Move to…",
                                         "<Primary><Alt>v TrustCopy: Paste from clipboard", desktop.ACCELS_END])
        i18n.set_language("de")
        desktop.install_scripts()  # what a language change does
        self.assertEqual(len(self.accels()), 5)  # replaced, not added to
        self.assertIn("<Primary><Alt>c TrustCopy: Kopieren nach …", self.accels())
        desktop.install_scripts(remove_only=True)
        self.assertEqual(self.accels(), [])
        desktop.install_scripts()  # enabled still: the next integrate brings them back
        self.assertEqual(len(self.accels()), 5)
        self.assertIsNone(desktop.set_option("shortcuts", False))
        self.assertFalse(desktop.ACCELS_FILE.exists())  # nothing but our block was in it

    def test_shortcuts_in_the_submenu_style_use_the_plain_names(self):
        desktop.set_option("menu", "folder")
        desktop.set_option("shortcuts", True)
        self.assertIn("<Primary><Alt>c Copy to…", self.accels())

    def test_a_shortcut_change_restarts_only_files_a_menu_change_every_file_manager(self):
        def changed():
            after = desktop.files_setup()
            return [fm for fm in after if after[fm] != before[fm]]

        before = desktop.files_setup()
        desktop.set_option("shortcuts", True)
        self.assertEqual(changed(), ["nautilus"])  # Nemo and Caja do not read our shortcuts
        before = desktop.files_setup()
        desktop.set_option("menu", "folder")
        self.assertEqual(changed(), ["nautilus", "nemo", "caja"])

    def test_shortcuts_leave_foreign_lines_alone_and_skip_keys_that_are_taken(self):
        desktop.ACCELS_FILE.parent.mkdir()
        desktop.ACCELS_FILE.write_text("<Control><alt>X My script\n# note\n<Primary>F2 Other")  # no final newline, another spelling
        note = desktop.set_option("shortcuts", True)
        self.assertIn("Ctrl+Alt+X", note)
        self.assertNotIn("Ctrl+Alt+C", note)
        lines = self.accels()
        self.assertEqual(lines[:3], ["<Control><alt>X My script", "# note", "<Primary>F2 Other"])
        self.assertEqual(sum("TrustCopy" in line and not line.startswith("#") for line in lines), 2)  # copy and paste, not move
        desktop.set_option("shortcuts", False)
        self.assertEqual(self.accels(), ["<Control><alt>X My script", "# note", "<Primary>F2 Other"])

    def test_shortcuts_skip_keys_that_gnome_uses(self):
        with mock.patch.object(desktop, "gnome_shortcuts", return_value={desktop.accel_key("<Alt><Control>v")}):
            note = desktop.set_option("shortcuts", True)
        self.assertIn("Ctrl+Alt+V", note)
        self.assertEqual(len([line for line in self.accels() if not line.startswith("#")]), 2)

    def test_chosen_keys_are_written_and_a_bad_choice_falls_back_to_the_default(self):
        desktop.set_option("shortcuts", True)
        self.assertIsNone(desktop.set_option("keys", {"copy": "<Control><Alt>k", "move": "nonsense", "paste": 5}))
        self.assertEqual(desktop.shortcut_keys(), {**desktop.ACCEL_DEFAULTS, "copy": "<Control><Alt>k"})
        self.assertIn("<Control><Alt>k TrustCopy: Copy to…", self.accels())
        self.assertIn("<Primary><Alt>x TrustCopy: Move to…", self.accels())
        self.assertEqual(json.loads(desktop.CONFIG_FILE.read_text())["keys"]["copy"], "<Control><Alt>k")

    def test_reset_switches_the_shortcuts_off_and_brings_the_default_keys_back(self):
        desktop.set_option("shortcuts", True)
        desktop.set_option("keys", {"copy": "<Control><Alt>k"})
        desktop.ACCELS_FILE.write_text(desktop.ACCELS_FILE.read_text() + "<Primary>F2 Mine\n")
        self.assertIsNone(desktop.reset_config())
        config = desktop.load_config()
        self.assertEqual((config["shortcuts"], config["keys"]), (False, {}))
        self.assertEqual(desktop.shortcut_keys(), desktop.ACCEL_DEFAULTS)
        self.assertEqual(self.accels(), ["<Primary>F2 Mine"])  # our block is gone, the user's line stays

    def test_key_problems(self):
        with mock.patch.object(desktop, "gnome_shortcuts", return_value={desktop.accel_key("<Super>t")}):
            self.assertIsNone(desktop.key_problem("<Primary><Alt>k", "copy"))
            self.assertIsNone(desktop.key_problem("<Primary><Alt>c", "copy"))  # its own key is no conflict
            self.assertTrue(desktop.key_problem("<Primary><Alt>x", "copy"))  # the key of another action
            self.assertTrue(desktop.key_problem("<Super>t", "copy"))  # GNOME's
            self.assertTrue(desktop.key_problem("<Primary>h", "copy"))  # Files' own
            self.assertTrue(desktop.key_problem("k", "copy"))  # a bare letter
            self.assertTrue(desktop.key_problem("<Shift>k", "copy"))
            desktop.ACCELS_FILE.parent.mkdir()
            desktop.ACCELS_FILE.write_text("<Control><Alt>K My script\n")
            self.assertTrue(desktop.key_problem("<Primary><Alt>k", "copy"))  # another script's

    def test_a_key_is_one_key_whatever_its_spelling(self):
        self.assertEqual(desktop.accel_key("<Control><Alt>X"), desktop.accel_key("<alt><Primary>x"))
        self.assertNotEqual(desktop.accel_key("<Primary>x"), desktop.accel_key("<Primary><Shift>x"))
        self.assertIsNone(desktop.accel_key("disabled"))
        self.assertIsNone(desktop.accel_key(""))
        self.assertEqual(desktop.accel_label("<Control><alt>x"), "Ctrl+Alt+X")
        self.assertEqual(desktop.accel_label("<shift><Primary>Page_Up"), "Ctrl+Shift+Page_Up")

    def test_keys_left_out_are_a_note_not_an_error(self):
        desktop.ACCELS_FILE.parent.mkdir()
        desktop.ACCELS_FILE.write_text("<Control><alt>X My script\n")
        note = desktop.set_option("shortcuts", True)
        self.assertIsInstance(note, desktop.Note)  # the installer must not take it for a failure
        with mock.patch.object(desktop, "install_scripts", return_value=note), mock.patch.object(sys, "argv", ["trustcopy", "integrate"]):
            import trustcopy
            self.assertEqual(trustcopy.main(), 0)
        with mock.patch.object(desktop, "install_scripts", return_value="disk full"), mock.patch.object(sys, "argv", ["trustcopy", "integrate"]):
            self.assertEqual(trustcopy.main(), 1)

    def test_switching_off_keeps_a_linked_shortcuts_file_a_link(self):
        real = Path(desktop.ACCELS_FILE.parent.parent, "dotfiles-accels")
        desktop.ACCELS_FILE.parent.mkdir()
        real.write_text("")  # nothing but our block will be in it, so switching off takes the branch that deletes a file
        desktop.ACCELS_FILE.symlink_to(real)
        desktop.set_option("shortcuts", True)
        self.assertIn(desktop.ACCELS_BEGIN, real.read_text())
        desktop.set_option("shortcuts", False)
        self.assertTrue(desktop.ACCELS_FILE.is_symlink())
        self.assertEqual(real.read_text(), "")

    @unittest.skipUnless(desktop.Gio and shutil.which("glib-compile-schemas"), "needs PyGObject and the schema compiler")
    def test_an_invalid_custom_shortcut_path_does_not_break_reading_the_keys(self):
        schemas = Path(tempfile.mkdtemp())  # a minimal copy of GNOME's media-keys schemas: the sandbox may have none
        self.addCleanup(shutil.rmtree, schemas)
        (schemas / "t.gschema.xml").write_text("""<schemalist>
<schema id="org.gnome.settings-daemon.plugins.media-keys" path="/m/"><key name="custom-keybindings" type="as"><default>[]</default></key></schema>
<schema id="org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"><key name="binding" type="s"><default>''</default></key></schema>
</schemalist>""")
        subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import desktop; from gi.repository import Gio\n"
                "Gio.Settings.new('org.gnome.settings-daemon.plugins.media-keys').set_strv('custom-keybindings', ['/no/trailing/slash', '/ok/custom0/'])\n"
                "Gio.Settings.new_with_path('org.gnome.settings-daemon.plugins.media-keys.custom-keybinding', '/ok/custom0/').set_string('binding', '<Control>j')\n"
                "print(desktop.accel_key('<Control>j') in desktop.gnome_shortcuts())")
        env = {**os.environ, "GSETTINGS_BACKEND": "memory", "GSETTINGS_SCHEMA_DIR": str(schemas)}
        done = subprocess.run([sys.executable, "-c", code, str(SRC)], env=env, capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "True"), done.stderr[-400:])

    @unittest.skipUnless(desktop.Gio and desktop.Gio.SettingsSchemaSource.get_default().lookup("org.gnome.desktop.wm.keybindings", True), "no GNOME schemas")
    def test_gnome_shortcuts_are_found(self):
        self.assertIn(desktop.accel_key("<Alt>F4"), desktop.gnome_shortcuts())  # closing a window

    def test_stale_entries_go_whatever_their_name_but_foreign_files_stay(self):
        desktop.SCRIPTS_DIR.mkdir()
        old = desktop.SCRIPTS_DIR / "TrustCopy: Old translation"
        old.write_text("#!/bin/sh\n")
        mine = desktop.SCRIPTS_DIR / "My own script"
        mine.write_text("#!/bin/sh\n")
        desktop.set_option("menu", "folder")
        folder = desktop.SCRIPTS_DIR / "TrustCopy"
        (folder / "Renamed entry").write_text(desktop.script_text("copy"))  # ours, named by an older translation
        (folder / "Not ours").write_text("#!/bin/sh\n")
        (folder / "My check").write_text(f'#!/bin/sh\n{desktop.LAUNCHER} copy -- "$@" >> /tmp/log\n')  # calls us, still not ours
        desktop.install_scripts()
        self.assertFalse(old.exists())
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["Copy to…", "Move to…", "My check", "Not ours", "Paste from clipboard"])
        desktop.set_option("menu", "scripts")  # the folder holds foreign files, so it stays
        self.assertTrue((folder / "Not ours").exists() and mine.exists())

    def test_entries_are_recognised_whichever_launcher_wrote_them(self):
        entry = desktop.SCRIPTS_DIR / "entry"
        desktop.SCRIPTS_DIR.mkdir()
        with mock.patch.object(desktop, "LAUNCHER", Path("/tmp/it's here/trustcopy")):  # a checkout run, an apostrophe in the path
            entry.write_text(desktop.script_text("copy"))
        self.assertTrue(desktop._ours(entry))
        self.assertFalse(desktop._ours(desktop.SCRIPTS_DIR))  # a folder
        entry.write_bytes(b"\xff\xfe\x00binary")
        self.assertFalse(desktop._ours(entry))
        entry.unlink()
        os.mkfifo(entry)  # open() would block on it for ever
        self.assertFalse(desktop._ours(entry))

    def test_entries_run_the_installed_launcher_not_the_process_that_wrote_them(self):
        desktop.install_scripts()
        for script in desktop.menu_entries():
            self.assertIn(str(desktop.LAUNCHER), script.read_text())
        self.assertEqual(desktop.LAUNCHER, Path.home() / ".local/bin/trustcopy")


class FakeGio:
    """Stands in for Gio and GLib in core: records what a file manager's restart does and keeps its callbacks."""

    class Error(Exception):
        message = "cannot run nautilus"

    SOURCE_REMOVE = False

    def __init__(self, fail_quit=False, fail_open=False):
        self.events, self.runs, self.timers, self.vanished, self.watches = [], [], [], {}, 0
        self.fail_quit, self.fail_open = fail_quit, fail_open
        self.Subprocess = types.SimpleNamespace(new=self.run)
        self.SubprocessFlags = types.SimpleNamespace(NONE=0)
        self.BusType = types.SimpleNamespace(SESSION=1)
        self.BusNameWatcherFlags = types.SimpleNamespace(NONE=0)

    def run(self, argv, flags):
        quit = argv[1] == "-q"
        if self.fail_quit if quit else self.fail_open:
            raise self.Error()
        self.events.append("quit" if quit else "open")
        self.runs.append(argv)

    def bus_watch_name(self, bus, name, flags, appeared, vanished):
        self.vanished[name] = vanished
        self.events.append("watch")
        self.watches += 1  # every watch gets its own id, as in GLib
        return self.watches

    def bus_unwatch_name(self, watch):
        self.events.append("unwatch")

    def timeout_add_seconds(self, seconds, function, *args):
        self.timers.append((function, args))


@unittest.skipUnless(desktop.Gio, "needs PyGObject")
class FilesRunning(unittest.TestCase):
    def says(self, owners=(), programs=("nautilus", "nemo", "caja"), fail=False):
        bus = mock.Mock()
        bus.call_sync.side_effect = lambda *args: desktop.GLib.Variant("(b)", (args[4].unpack()[0] in owners,))
        if fail:  # the bus cannot be reached
            bus.call_sync.side_effect = desktop.GLib.Error("no bus")
        with mock.patch.object(desktop.Gio, "bus_get_sync", return_value=bus):
            return desktop.files_running(list(programs))

    def test_the_restart_is_only_offered_to_the_file_managers_that_run(self):
        self.assertEqual(self.says({"org.gnome.Nautilus"}), ["nautilus"])
        self.assertEqual(self.says({"org.Nemo", "org.mate.Caja"}), ["nemo", "caja"])  # a Nemo user gets no Nautilus opened
        self.assertEqual(self.says(), [])
        self.assertEqual(self.says({"org.Nemo"}, ["nautilus"]), [])  # only the ones whose setup changed
        self.assertEqual(self.says(fail=True), ["nautilus"])  # no answer: ask for Files as before, never for a fork


class FakeApp:
    def __init__(self):
        self.holds = self.releases = 0

    def hold(self):
        self.holds += 1

    def release(self):
        self.releases += 1


class RestartFiles(unittest.TestCase):
    def setUp(self):
        self.bus, self.app, self.toasts = FakeGio(), FakeApp(), []
        for name in ("Gio", "GLib"):
            patcher = mock.patch.object(desktop, name, self.bus)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(desktop.waits.clear)

    def restart(self, programs=("nautilus",)):
        desktop.restart_files(self.app, self.toasts.append, list(programs))

    def fire_timer(self, n=0):
        function, args = self.bus.timers[n]
        self.assertIs(function(*args), False)  # the timer must not repeat

    def files_quits(self, fm="nautilus"):
        name = desktop.FILE_MANAGERS[fm]
        self.bus.vanished[name](None, name)

    def balanced(self):
        self.assertEqual(self.app.holds, self.app.releases)
        self.assertEqual(desktop.waits, [])

    def test_files_quits_first_and_the_late_timer_does_nothing(self):
        self.restart()
        self.assertEqual((self.bus.events, self.app.holds, self.app.releases), (["quit", "watch"], 1, 0))
        self.files_quits()
        self.assertEqual(self.bus.events, ["quit", "watch", "unwatch", "open"])
        self.fire_timer()
        self.assertEqual((self.bus.events[-1], self.app.releases), ("open", 1))
        self.balanced()

    def test_the_timer_gives_up_first_and_a_late_answer_opens_nothing(self):
        self.restart()
        self.fire_timer()
        self.assertEqual(self.bus.events, ["quit", "watch", "unwatch"])
        self.files_quits()
        self.assertNotIn("open", self.bus.events)
        self.balanced()

    def test_a_second_request_waits_for_the_first(self):
        self.restart()
        self.restart()
        self.assertEqual((self.bus.events.count("quit"), self.app.holds), (1, 1))
        self.files_quits()
        self.balanced()

    def test_an_old_timer_does_not_end_a_newer_wait(self):
        self.restart()
        self.files_quits()
        self.restart()
        self.fire_timer(0)  # the timer of the first restart
        self.assertEqual(len(desktop.waits), 1)
        self.files_quits()
        self.assertEqual(self.bus.events.count("open"), 2)
        self.balanced()

    def test_each_fork_is_quit_and_opened_again_by_itself(self):
        self.restart(["nemo", "caja"])
        self.assertEqual((self.bus.runs, self.app.holds), ([["nemo", "-q"], ["caja", "-q"]], 2))
        self.files_quits("caja")
        self.assertEqual(self.bus.runs[2:], [["caja", os.path.expanduser("~")]])  # the program itself: Caja then draws the desktop again
        self.restart(["nemo"])  # Nemo is still on its way out
        self.files_quits("nemo")
        self.assertEqual(self.bus.runs[3:], [["nemo", os.path.expanduser("~")]])
        self.balanced()

    def test_nautilus_that_cannot_run_is_reported_and_nothing_is_held(self):
        self.bus.fail_quit = True
        self.restart()
        self.assertEqual((self.toasts, self.app.holds, self.bus.events), (["cannot run nautilus"], 0, []))
        self.balanced()

    def test_a_failing_reopen_is_reported_and_the_hold_is_released(self):
        self.bus.fail_open = True
        self.restart()
        self.files_quits()
        self.assertEqual(self.toasts, ["cannot run nautilus"])
        self.balanced()


class Translations(unittest.TestCase):
    def test_every_po_matches_the_strings_in_the_code(self):
        wanted = set()
        for path in SRC.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("_", "N_", "_n")
                        and node.args and isinstance(node.args[0], ast.Constant)):
                    wanted.add(node.args[0].value)
        for po in sorted(i18n.PO_DIR.glob("*.po")):
            catalog = i18n.load_po(po)
            have = set(catalog) - {""}  # "" is the header
            self.assertEqual(wanted - have, set(), f"missing in {po.name}")
            self.assertEqual(have - wanted, set(), f"unused in {po.name}")
            nplurals = int(re.search(r"nplurals=(\d+)", catalog[""]).group(1))
            for msgid, forms in catalog.items():
                if isinstance(forms, list):
                    self.assertEqual(len(forms), nplurals, f"{po.name}: wrong number of forms in {msgid!r}")
            self.assertIn(f"Project-Id-Version: TrustCopy {core.__version__}\n", catalog[""], po.name)
        self.assertIn(f'"Project-Id-Version: TrustCopy {core.__version__}\\n"', (i18n.PO_DIR / "trustcopy.pot").read_text())

    def test_xgettext_sees_every_string(self):
        """xgettext skips f-strings: a _("...") inside one drops out of the template at the next refresh, and msgmerge then retires its translations."""
        for path in SRC.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.JoinedStr):
                    hidden = [c.args[0].value for c in ast.walk(node) if isinstance(c, ast.Call) and getattr(c.func, "id", "") in ("_", "N_", "_n")
                              and c.args and isinstance(c.args[0], ast.Constant)]
                    self.assertEqual(hidden, [], f"{path.name}: translated inside an f-string")

    def test_plural_forms_follow_the_language_rule(self):
        polish = {"": "Plural-Forms: nplurals=3; plural=(n==1 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);\n",
                  "{n} file": ["{n} plik", "{n} pliki", "{n} plików"]}
        with mock.patch.object(i18n, "_catalog", lambda lang: polish):
            i18n._plural_rule.cache_clear()
            self.addCleanup(i18n._plural_rule.cache_clear)
            self.assertEqual([i18n._n("{n} file", "{n} files", n) for n in (1, 2, 5, 22, 25)],
                             ["{n} plik", "{n} pliki", "{n} plików", "{n} pliki", "{n} plików"])
        i18n.set_language("en")
        self.assertEqual((i18n._n("{n} file", "{n} files", 1), i18n._n("{n} file", "{n} files", 2)), ("{n} file", "{n} files"))
        i18n.set_language("de")
        self.addCleanup(i18n.set_language, "en")
        self.assertEqual(i18n._n("{n} file copied successfully", "{n} files copied successfully", 1), "{n} Datei erfolgreich kopiert")
        self.assertEqual(i18n._n("{n} file copied successfully", "{n} files copied successfully", 4), "{n} Dateien erfolgreich kopiert")

    def test_every_language_picks_a_form_for_every_number(self):
        self.addCleanup(i18n.set_language, "en")
        for lang in i18n.languages():
            i18n.set_language(lang)
            for n in range(0, 130):
                self.assertIn("{n}", i18n._n("{n} file skipped", "{n} files skipped", n), f"{lang} {n}")

    def test_placeholders_and_menu_titles(self):
        def fields(text):
            return sorted(re.findall(r"\{[^}]*\}", text))

        for po in sorted(i18n.PO_DIR.glob("*.po")):
            for msgid, msgstr in i18n.load_po(po).items():
                for form in msgstr if isinstance(msgstr, list) else [msgstr]:
                    if msgid:
                        self.assertEqual(fields(msgid), fields(form), f"{po.name}: placeholders differ in {msgid!r}")
        for lang in i18n.languages():
            for title in desktop.SCRIPT_ACTIONS.values():
                self.assertNotIn("/", i18n.translate(title, lang))  # a slash would split the file name


class InstallerTests(unittest.TestCase):
    def test_the_installer_fetches_the_release_it_belongs_to(self):
        text = (SRC.parent / "install.sh").read_text()
        self.assertEqual(re.search(r'^VERSION="([^"]+)"', text, re.M).group(1), core.__version__)
        self.assertNotIn("refs/heads", text)  # a branch moves on; a release tag does not
        # a tag can be moved too: the release workflow pins the commit in the copy it attaches, which must then fetch that commit
        self.assertEqual(len(re.findall(r'^COMMIT=""', text, re.M)), 1)
        self.assertIn("archive/${COMMIT:-refs/tags/v$VERSION}.tar.gz", text)
        self.assertIn('s/^COMMIT=\\"\\"/COMMIT=', (SRC.parent / ".github/workflows/release.yml").read_text())

    def test_install_and_purge_in_a_home_of_their_own(self):
        with tempfile.TemporaryDirectory() as home:
            env = {k: v for k, v in os.environ.items() if k not in ("XDG_DATA_HOME", "XDG_CONFIG_HOME")} | {"HOME": home, "NO_COLOR": "1"}
            done = subprocess.run(["bash", str(SRC.parent / "install.sh")], env=env, capture_output=True, text=True)
            if "TrustCopy needs GTK" in done.stdout + done.stderr:
                self.skipTest("the installer stops without GTK 4 and libadwaita")
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            app, launcher = Path(home, ".local/share/trustcopy"), Path(home, ".local/bin/trustcopy")
            self.assertEqual(subprocess.run([str(launcher), "--version"], env=env, capture_output=True, text=True).stdout.strip(), f"TrustCopy {core.__version__}")
            self.assertEqual(sorted(p.name for p in app.glob("*.py")), sorted(p.name for p in SRC.glob("*.py")))
            self.assertEqual(len(list(Path(home, ".local/share/nautilus/scripts").iterdir())), 3)
            Path(home, ".config/trustcopy").mkdir(parents=True)
            Path(home, ".config/trustcopy/settings.json").write_text("{}")
            done = subprocess.run(["bash", str(app / "install.sh"), "--purge"], env=env, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            left = sorted(str(p.relative_to(home)) for p in Path(home).rglob("*") if not p.is_dir())
            self.assertEqual(left, [x for x in left if x.endswith("mimeinfo.cache")])  # only what update-desktop-database writes


if __name__ == "__main__":
    unittest.main()
