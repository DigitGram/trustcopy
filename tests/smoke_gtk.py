# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DigitGram
"""GTK smoke test: after a real copy the progress window must have laid out its bars (a bar can be there and 0 px high).
Run: xvfb-run -a python3 tests/smoke_gtk.py   (needs xvfb, python3-gi, python3-gi-cairo, gir1.2-adw-1)"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

SRC = Path(__file__).resolve().parent.parent / "src"


def problems(panel):
    """What is wrong with the graph and the bar of a finished job."""
    found = [f"{name} has no height" for name in ("graph", "result", "tiles") if getattr(panel, name).get_height() <= 0]
    if not panel.verdict.get_label() or panel.tile_values[0].get_label() == "":
        found.append("the result or the tiles are empty")
    if bool(panel.big_speed.get_label()) != bool(sys.modules["ui_common"].avg_write(panel.job)):  # the big figure is the average the tiles show; below 1 MiB there is none
        found.append(f"the big speed {panel.big_speed.get_label()!r} does not match the tiles")
    for tile, value in zip(panel.tile_boxes[1:3], panel.tile_values[1:3]):  # a speed shown as "–" says why, a figure needs no explanation
        if bool(tile.get_tooltip_text()) != (value.get_label() == "–"):
            found.append(f"the speed tile {value.get_label()!r} has the tooltip {tile.get_tooltip_text()!r}")
    panel.files_win.show_kind("failed")
    panel.toggle_files()  # opening the list always starts on "All", whichever tab was left on
    if panel.files_win.kind != "all" or not panel.files_win.buttons["all"].get_active():
        found.append("the file window does not open on All")
    panel.files_win.refresh()
    if panel.files_win.tab_labels["all"].get_label().split()[-1] != str(len(panel.entries)):
        found.append(f"the file window's tabs or footer are off: {panel.files_win.tab_labels['all'].get_label()!r}")
    if any(e.result and e.result.status == "copied" for e in panel.entries.values()):
        found.append("a row still says Copied for a copy that was thrown away")
    if any(w.get_visible() for w in (panel.top,)):  # the bar, the figures and the file in flight sit inside it
        found.append("the running widgets stay after the job")
    ok, circle = panel.badge.compute_bounds(panel.circle), panel.circle.get_width()
    if not ok[0] or abs(ok[1].get_x() * 2 + ok[1].get_width() - circle) > 2:
        found.append("the result icon is not in the middle of its circle")
    if panel.circle.get_width() != panel.circle.get_height():
        found.append("the result circle is not round")
    if panel.first_title.get_label() != sys.modules["i18n"]._("Moving" if panel.job.move else "Copying"):
        found.append(f"step 1 is called {panel.first_title.get_label()!r}")
    if panel.complete and panel.job.check_pass and " · " not in panel.steps[1][2].get_label():
        found.append(f"step 2 shows no time: {panel.steps[1][2].get_label()!r}")
    if panel.complete and not panel.step_rev.get_child() is panel.step_row:
        found.append("the steps are lost after the job is done")  # they fade out after a moment, so only their widget is checked
    try:
        import cairo
        cr = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 400, 120))
        panel.draw(panel.graph, cr, 400, 120)
        if not panel.graph.get_tooltip_text():  # this job moves 100 KB: a line without a figure, which must say why
            found.append("the graph shows no speed and does not say why")
    except Exception as e:  # noqa: BLE001 - whatever breaks drawing is a failure of this test
        found.append(f"drawing the graphs: {e!r}")
    return found


def fake_area(tips=None):
    """Stands in for the graph's drawing area: its colour and its tooltip."""
    tips = [None] if tips is None else tips
    return NS(get_color=lambda: NS(red=1, green=1, blue=1), get_tooltip_text=lambda: tips[-1], set_tooltip_text=tips.append)


def descendants(widget):
    """Every widget below `widget`, depth first."""
    child = widget.get_first_child()
    while child:
        yield child
        yield from descendants(child)
        child = child.get_next_sibling()


def child(tmp):
    """Runs one copy in a window and checks the window once the copy is over, again with step 2 picked."""
    sys.path.insert(0, str(SRC))
    import desktop, ui_common as common, ui_dialogs as dialogs, ui_graph as graph, ui_gtk
    from ui_panel import Panel
    from gi.repository import Adw, GLib, Gtk

    segs = []
    pen = NS(curve_to=lambda *a: segs.append(a), line_to=lambda *a: segs.append(a))
    pts = [(0, 0), (10, 10), (20, 30), (30, 30), (40, 20), (50, 25)]
    graph.curve(pen, pts)
    assert all(min(y0, y1) <= c <= max(y0, y1) for (_, y0), (_, y1), (_, c1, _, c2, *_) in zip(pts, pts[1:], segs) for c in (c1, c2)), "overshoot"
    assert segs[1][1] > 10 and segs[2][1] == 30 and segs[4][1] == 20, segs  # a slope stays a slope (no step), a peak and a dip are level
    def drawn(job, active=False, samples=((0.0, 5.0), (1.0, 5.0))):  # the texts a finished job shows in its graph
        cr = MagicMock()
        cr.text_extents.return_value.width = 100
        job.__dict__.setdefault("verify", False), job.__dict__.setdefault("writing", NS(secs=0.0))
        Panel.draw(NS(speeds=lambda window=1, track=None: list(samples), window=1, reading=False, peek=False, track=[], active=active, job=job, marks=[], written_share=lambda: 0), fake_area(), cr, 600, 120)
        return [c.args[0] for c in cr.show_text.call_args_list][1:] + [c.args[0] for c in cr.text_path.call_args_list]  # the first text is the graph's title; the scale labels go through text_path (a halo)
    renamed = NS(move=True, written=0, results=[NS(status="ok")])
    assert drawn(renamed) == ["Moved instantly", "same partition, nothing to copy"], drawn(renamed)
    for other in (NS(move=False, written=0, results=[NS(status="ok")]), NS(move=True, written=5, results=[NS(status="ok")]),
                  NS(move=True, written=0, results=[NS(status="skipped")])):  # a copy, a move that copied, a move that moved nothing
        shown = drawn(other)
        assert {"0 s", "1 s"} <= set(shown) and "Moved instantly" not in shown, shown  # the time scale and the last speed stay
    burst = [(0.0, 450e6)] + [(float(i), 10e6) for i in range(1, 31)]  # the cache takes the first seconds at once, the stick the rest
    shown = drawn(NS(move=False, written=1, results=[]), samples=burst)
    assert "60\xa0MB/s" in shown and "450\xa0MB/s" not in shown, shown
    rows = NS(entries={}, keys={})  # a source that is in the job twice has two rows, and each result its own
    first, second = Panel.add(rows, "a", 1, 0), Panel.add(rows, "a", 1, 1)
    one, two = NS(status="copied"), NS(status="copied")
    assert Panel.row(rows, "a") is first and Panel.row(rows, "missing") is None
    first.result = one
    assert Panel.row(rows, "a", two) is second and Panel.row(rows, "a", one) is first  # the next result takes the row that waits
    second.result = two
    assert Panel.row(rows, "a", two) is second and len(rows.entries) == 2 and first.key != second.key
    assert common.outcome(NS(fatal="", cancelled=False, move=True, results=[NS(status="skipped")]), 0, [])[0] == sys.modules["i18n"]._("Skipped")  # moved onto itself: not "0 files moved"
    assert common.rate(2 << 20, NS(secs=2.0)).endswith("/s") and common.rate(1000, NS(secs=1.0)) == "–"
    with tempfile.TemporaryDirectory() as d:  # the conflict dialog says how the new file differs from the existing one
        new, old = Path(d, "new"), Path(d, "old")
        new.write_bytes(b"x" * 200), old.write_bytes(b"x" * 100)
        os.utime(new, (2000, 2000)), os.utime(old, (1000, 1000))
        kind = lambda a, b: sys.modules["core"].compare(str(a), str(b))[1]
        assert kind(new, old) == "good" and kind(old, new) == "bad", (kind(new, old), kind(old, new))  # newer and larger / older and smaller
        os.utime(old, (2000, 2000))
        old.write_bytes(b"y" * 200)
        os.utime(old, (2000, 2000))
        assert kind(new, old) == "same", "equal in size and time"
        for _kind in ("same", "good", "bad"):  # builds with each of the three hints
            dialogs.conflict_dialog(str(new), str(old), lambda *a: None)
            os.utime(old, (1000, 1000)) if _kind == "same" else os.utime(old, (9000, 9000))
    pick = sys.modules["ui_panel"].shown_file
    assert pick({"big.iso": "copy", "a.sfv": "copy"}, "copy", "a.sfv") == "big.iso"  # the last one started is not the one that runs longest
    assert pick({"x.mkv": "verify", "big.iso": "copy"}, "copy", "x.mkv") == "big.iso" and pick({}, "copy", "last.bin") == "last.bin"
    def steps_of(job, **now):  # (css class, number) of each step after _show_steps
        rows = [(MagicMock(), MagicMock(), MagicMock()) for _step in range(3)]
        Panel._show_steps(NS(complete=False, picked=None, steps=rows, joins=[MagicMock()], reading=False, active=False, verify_secs=0.0, tile_boxes=[MagicMock() for _t in range(4)], copy_info=(106e6, 16.0), clock=lambda: 20.0, paused=0.0, graph_time=lambda: 5.0, **now), job)
        return [(item.set_css_classes.call_args.args[0][:1], "✓" if num.set_visible_child_name.call_args.args[0] == "tick" else num.get_child_by_name.return_value.set_label.call_args.args[0], item.set_visible.call_args and item.set_visible.call_args.args[0]) for item, num, _d in rows]
    running = NS(check_pass=False, verify=True, algo="crc32", written=0)
    assert [c[:2] for c in steps_of(running)] == [(["k-step-active"], "1"), (["k-step-todo"], "2"), (["k-step-todo"], "3")]
    checking = NS(check_pass=True, verify=True, algo="crc32", written=0)
    assert [c[:2] for c in steps_of(checking)] == [(["k-step-done"], "✓"), (["k-step-active"], "2"), (["k-step-todo"], "3")]
    two = steps_of(NS(check_pass=False, verify=False, algo="crc32", written=0))
    assert two[1][2] is False and two[2][1] == "2", two  # no verification: two steps, the last one is the second
    def sums_end(**result):  # the checksum file is done: does the computer still go off?
        panel = NS(sums_row=MagicMock(), sums_label=MagicMock(), close_after_sums=False, power_after="poweroff", confirm_power=MagicMock(), toasts=MagicMock())
        Panel.sums_done(panel, **result)
        return panel.confirm_power.called, panel.power_after
    assert sums_end(path="/x/SHA256SUMS") == (True, "poweroff") and sums_end() == (True, "poweroff"), "a written or skipped checksum file keeps the power action"
    assert sums_end(error="disk full") == (False, ""), "the computer must not go off after a checksum file that could not be written"
    for reading, marked in ((False, 1), (True, 2)):  # the speed tile of the graph on show is the marked one
        tiles = [MagicMock() for _t in range(4)]
        Panel._show_steps(NS(complete=True, picked=3, steps=[(MagicMock(), MagicMock(), MagicMock()) for _s in range(3)], joins=[MagicMock()], reading=reading, tile_boxes=tiles, active=False, verify_secs=0.0,
                             copy_info=(1, 1.0), clock=lambda: 2.0, paused=0.0, graph_time=lambda: 1.0), running)
        assert [i for i in (1, 2) if tiles[i].add_css_class.called] == [marked] and [i for i in (1, 2) if tiles[i].remove_css_class.called] == [3 - marked]
    for paused, check_pass, kind in ((False, False, "copy"), (False, True, "verify"), (True, True, "paused")):  # the bar: step colour, the accent colour while paused
        shell = NS(bar=MagicMock(), percent=MagicMock())
        Panel._show_bars(shell, NS(total=1, paused=paused, check_pass=check_pass), 0.5)
        assert [c.args[0] for c in shell.bar.add_css_class.call_args_list] == ["k-bar-" + kind], (kind, shell.bar.add_css_class.call_args_list)
    swap = NS(track=[(0.0, 0), (9.0, 50)], lead=2.0, marks=[1.0], graph_time=lambda: 9.0, all_written=True, reading=False)
    Panel._start_readback(swap, NS(written=50))
    assert (swap.reading, swap.track, swap.marks, swap.all_written, swap.copy_info) == (True, [(0.0, 0)], [], False, (50, 9.0))
    assert swap.saved == ([(0.0, 0), (9.0, 50)], 2.0, [1.0]) and swap.lead == 11.0, swap.saved  # the copy graph waits, the new one counts its seconds from now
    folder, rows, more = common.leftover_rows([("/x/y/a.trustpart", 5), ("/x/y/b.trustpart", 7)])  # one folder: said once, the rows carry only the names
    assert (folder, rows, more) == ("/x/y", [("a.trustpart", None, 5, "/x/y/a.trustpart"), ("b.trustpart", None, 7, "/x/y/b.trustpart")], None), (folder, rows, more)
    folder, rows, more = common.leftover_rows([("/x/y/a.trustpart", 5), ("/x/z/b.trustpart", 7)])  # several folders: each row says where
    assert folder is None and [r[:2] for r in rows] == [("a.trustpart", "/x/y"), ("b.trustpart", "/x/z")], rows
    folder, rows, more = common.leftover_rows([(f"/x/f{n}.trustpart", 10) for n in range(1000)])
    assert folder == "/x" and len(rows) == 4 and rows[0][0] == "f0.trustpart" and more[:2] == (996, 9960), (len(rows), more[:2])  # a thousand stay four rows and a count with its size
    assert more[2].split("\n")[0] == "f4.trustpart" and more[2].split("\n")[-1] == "…" and len(more[2].split("\n")) == 21  # its tooltip names the next twenty
    assert common.leftover_rows([(f"/x/f{n}.trustpart", 1) for n in range(5)])[2] is None and common.leftover_rows([(f"/x/f{n}.trustpart", 1) for n in range(6)])[2][0] == 2  # never "and 1 more"
    for missing, total in ((1, 1), (3, 10)):  # the two checksum-file questions build, with and without the middle answer
        dialogs.sums_gap_dialog(missing, total, 5 << 20, lambda choice: None)
    dialogs.sums_conflict_dialog("/x/SHA256SUMS", {"a": "0" * 64}, 1700000000, 12, lambda how: None)
    for pairs in ([("/x/a.trustpart", 5)], [(f"/x/f{n}.trustpart", 10) for n in range(40)] + [("/y/z.trustpart", 1)]):  # the dialog builds, one folder or many, few or many
        answers = []
        dialogs.cleanup_dialog(pairs, answers.append)
    still = NS(sums_reading=False, sums_label=MagicMock(), job=NS(sum_read=1, sum_gap=lambda: ([], 1)))  # the tick that comes after the end must leave the result alone
    assert Panel.sums_progress(still, 1) is False and not still.sums_label.set_label.called
    running = NS(sums_reading=True, sums_label=MagicMock(), job=NS(sum_read=50))
    assert Panel.sums_progress(running, 100) is True and "50" in running.sums_label.set_label.call_args.args[0]
    due = common.power_due  # the computer is switched off only when it was chosen, everything went well and nothing else follows
    assert due("poweroff", "success", False) == "poweroff" and due("suspend", "success", False) == "suspend"
    assert not (due("poweroff", "warning", False) or due("poweroff", "error", False) or due("poweroff", "success", True) or due("keep", "success", False) or due("always", "success", False))
    went = []
    cancelled = dialogs.power_dialog("poweroff", lambda: went.append("cancelled"))
    cancelled.emit("response::cancel", "cancel")
    cancelled.emit("response::now", "now")  # after a Cancel nothing happens any more
    pressed = dialogs.power_dialog("suspend", lambda: went.append("pressed"))
    pressed.emit("response::now", "now")
    pressed.emit("response::now", "now")  # and only once
    assert went == ["pressed"], went
    dialogs.power_dialog("poweroff", lambda: went.append("counted"), seconds=1)  # runs out by itself: looked at in the probe
    with tempfile.TemporaryDirectory() as d:  # the offer to delete the leftovers of a crashed job
        part = Path(d, "x.bin.trustpart")
        part.write_bytes(b"half")
        offer = NS(leftovers=[], sweep=MagicMock(), is_visible=lambda: True, sweep_answered=lambda yes: None)
        Panel.offer_sweep(offer, [(str(part), 4)])
        assert offer.leftovers == [(str(part), 4)] and offer.sweep.set_visible.call_args.args[0] is True and "1" in offer.sweep.set_label.call_args.args[0]
        Panel.offer_sweep(NS(is_visible=lambda: False, sweep=offer.sweep, leftovers=[]), [(str(part), 4)])  # a window that is gone is not asked
        assert offer.sweep.set_visible.call_count == 1
        Panel.sweep_answered(offer, False)
        assert part.exists(), "No must delete nothing"
        Panel.sweep_answered(offer, True)
        assert not part.exists() and offer.sweep.set_sensitive.call_args.args[0] is False, "Yes deletes, and the button turns into the answer"
        busy = NS(get_application=lambda: NS(running=object(), waiting=[]), dest=d)
        Panel.look_for_leftovers(busy, NS(results=[]))  # another job runs: its own temp files must not be offered (a thread would be started otherwise)
    for active, reading, kept, shown in ((True, True, 0.0, "7 s"), (False, False, 9.0, "9 s"), (True, False, 0.0, "")):  # step 2 counts while it runs, then keeps its time
        rows = [(MagicMock(), MagicMock(), MagicMock()) for _s in range(3)]
        Panel._show_steps(NS(complete=False, picked=None, steps=rows, joins=[MagicMock()], reading=reading, active=active, verify_secs=kept, tile_boxes=[MagicMock() for _t in range(4)], copy_info=(1, 1.0),
                             clock=lambda: 2.0, paused=0.0, graph_time=lambda: 7.0), NS(check_pass=True, verify=True, written=1, algo="sha256"))
        assert rows[1][2].set_label.call_args.args[0].endswith(shown) and (" · " in rows[1][2].set_label.call_args.args[0]) == bool(shown), rows[1][2].set_label.call_args
    pick = NS(active=False, complete=True, job=None, write_data=([1], [2]), read_data=([3], [4]), picked=None, reading=False, track=None, marks=None, _show_steps=MagicMock(), _show_info=MagicMock(), graph=MagicMock(), tile_boxes=[MagicMock() for _t in range(4)])
    Panel.show_step(pick, 2)
    assert (pick.picked, pick.reading, pick.track, pick.marks) == (2, True, [3], [4])  # step 2 shows the read graph
    Panel.show_step(pick, 3)
    assert (pick.picked, pick.reading, pick.track) == (3, False, [1])
    early = NS(active=True, reading=False, complete=False, saved=([1], 0, []), peek=False, picked=None, job=None, _show_steps=MagicMock(), _show_info=MagicMock(), graph=MagicMock())
    for n in (2, 3):  # while copying only step 1 can be looked at
        Panel.show_step(early, n)
    assert early.picked is None and not early._show_steps.called
    early.reading = True
    Panel.show_step(early, 3)  # while verifying step 3 is not there yet, step 2 is
    assert early.picked is None and not early._show_steps.called
    Panel.show_step(early, 2)
    assert early._show_steps.called
    pick.read_data = None
    Panel.show_step(pick, 2)
    assert pick.picked == 3  # no read graph (no verifying): the click does nothing
    pick.active, pick.saved, pick.peek = True, None, False
    Panel.show_step(pick, 1)
    assert (pick.picked, pick.peek) == (3, False)  # copying: only the live graph
    pick.saved, pick.reading = ([1], 5, [2]), True  # verifying
    Panel.show_step(pick, 1)
    assert (pick.picked, pick.peek) == (1, True)  # verifying: step 1 shows the copy graph
    Panel.show_step(pick, 2)
    assert (pick.picked, pick.peek) == (None, False)  # any other step: back to the live one
    seen = []
    Panel.draw(NS(speeds=lambda window=1, track=None: seen.append(track) or [(0.0, 1.0), (1.0, 1.0)], window=1, reading=True, peek=True, saved=([7], 0, [8]), track=[9], marks=[], active=True, job=NS(move=False, written=1, paused=False, results=[], readback=0, reading=NS(secs=0.0), writing=NS(secs=0.0))),
               fake_area(), MagicMock(), 600, 120)
    assert seen == [[7]], seen  # the copy graph, not the track under way
    for reading, colour in ((True, graph.BLUE), (False, graph.GREEN)):  # the read graph is blue, the write graph green
        cr = MagicMock()
        Panel.draw(NS(speeds=lambda window=1, track=None: [(0.0, 1.0), (1.0, 1.0)], window=1, reading=reading, peek=False, track=[], marks=[], active=True, job=NS(paused=False)),
                   fake_area(), cr, 600, 120)
        assert any(c.args[:3] == colour and c.args[3] == 1 for c in cr.set_source_rgba.call_args_list), (reading, cr.set_source_rgba.call_args_list)
    assert graph.smoothed(0.0, 100.0, 1) == 100.0 and graph.smoothed(100.0, 0.0, 2) == 50.0 and abs(graph.smoothed(100.0, 0.0, 50) - 90) < 1e-9
    assert [graph.nice_step(p) for p in (520e6, 100e6)] == [150e6, 30e6] and graph.nice_step(4.2) * 4 >= 4.2
    track = NS(track=[(0.0, 0), (1.0, 3000), (2.0, 6000)])  # bytes written so far
    assert Panel.speeds(track) == [(0.0, 0.0), (1.0, 3000), (2.0, 3000)], Panel.speeds(track)  # the last second
    assert Panel.speeds(track, 3) == [(0.0, 0.0), (1.0, 1000), (2.0, 2000)]  # a longer window: a slower start from 0
    assert Panel.speeds(NS(track=[(0.0, 0)])) == [] and Panel.speeds(NS(track=[(0.0, 0), (0.5, 100)])) == [(0.0, 0.0), (0.5, 100.0)]
    flat, spike = [(i * 0.25, 5.0) for i in range(40)], [(i * 0.25, 10.0 if i == 20 else 0.0) for i in range(41)]
    soft = graph.soften(spike)  # a burst becomes a low hump over its neighbours; a flat run and the times stay, and so does an empty list
    assert all(abs(v - 5.0) < 1e-9 for _t, v in graph.soften(flat)) and [t for t, _v in soft] == [t for t, _v in spike] and graph.soften([]) == []
    assert max(v for _t, v in soft) < 3 and soft[17][1] > 0 and soft[0][1] == 0.0, soft
    assert Panel.graph_time(NS(clock=lambda: 10.0, paused=2.0, pause_t=0.0, lead=None)) == 8.0
    assert Panel.graph_time(NS(clock=lambda: 10.0, paused=2.0, pause_t=0.0, lead=3.0)) == 5.0  # the scan before the first byte is no part of the graph
    assert abs(Panel.graph_time(NS(clock=lambda: 10.0, paused=2.0, pause_t=time.monotonic() - 3, lead=None)) - 5.0) < 0.1  # a pause under way stands still too
    assert common.avg_write(NS(written=2 << 20, writing=NS(secs=2.0))) == 1 << 20 and common.avg_write(NS(written=1000, writing=NS(secs=1.0))) == 0.0
    done = NS(move=False, written=10 << 20, writing=NS(secs=1.0), results=[NS(status="ok")])
    quick = NS(move=False, written=2 << 20, writing=NS(secs=0.01), results=[NS(status="ok")])
    assert drawn(quick, active=True, samples=()) == []  # still running: nothing to draw yet
    cr = MagicMock()
    cr.text_extents.return_value.width = 100
    Panel.draw(NS(speeds=lambda window=1, track=None: [], window=1, reading=False, peek=False, track=[], active=False, job=quick, marks=[]), fake_area(), cr, 600, 120)
    assert cr.curve_to.call_args_list[-1].args[4] == 600, cr.curve_to.call_args_list  # that flat line ends at the right edge
    fast = NS(move=False, written=100 << 20, writing=NS(secs=0.4), results=[NS(status="ok")])  # 262 MB/s in 0.4 s: the 1 s window would show a hump of 100 MB/s
    cr = MagicMock()
    cr.text_extents.return_value.width = 100
    Panel.draw(NS(speeds=lambda window=1, track=None: [(0.0, 0.0), (0.2, 50e6), (0.4, 100e6)], window=1, reading=False, peek=False, track=[], active=False, job=fast, marks=[], written_share=lambda: 0), fake_area(), cr, 600, 120)
    assert len({c.args[5] for c in cr.curve_to.call_args_list}) == 1, "a step within the window draws flat at its average"
    tiny = NS(move=False, written=1000, writing=NS(secs=0.01), results=[NS(status="ok")])
    nothing = NS(move=False, written=0, writing=NS(secs=0.0), results=[NS(status="skipped")])
    for job, line in ((tiny, True), (nothing, False)):  # below 1 MiB: a line without a figure; nothing written: the grid stays empty
        cr = MagicMock()
        cr.text_extents.return_value.width = 100
        tips = [None]
        Panel.draw(NS(speeds=lambda window=1, track=None: [], window=1, reading=False, peek=False, track=[], active=False, job=job, marks=[]), fake_area(tips), cr, 600, 120)
        assert (cr.curve_to.call_args_list[-1].args[4] == 600 if line else not cr.curve_to.called), (line, cr.curve_to.call_args_list)
        assert bool(tips[-1]) == line, (line, tips)  # the line without a figure says why; an empty grid has nothing to explain
        assert [c.args[0] for c in cr.show_text.call_args_list][1:] + [c.args[0] for c in cr.text_path.call_args_list] == [], (cr.show_text.call_args_list, cr.text_path.call_args_list)  # no speed, no scale

    cr = MagicMock()  # the speed labels at the left edge of a 100 px graph: one per grid line (25, 50, 75)
    graph.draw_speed_scale(cr, NS(red=1, green=1, blue=1), 100, 150e6)
    assert len(cr.text_path.call_args_list) == 3, cr.text_path.call_args_list
    assert "Paused" in drawn(NS(move=False, written=1, paused=True, results=[]), active=True)  # the graph says it stands still
    for flying, waited in (({}, 1.0), ({"a": "copy"}, 0.0)):  # a question stops the clock only when nothing else is in flight
        panel = NS(job=NS(phases=flying), waited=0.0)
        Panel.count_wait(panel, time.monotonic() - 1)
        assert abs(panel.waited - waited) < 0.1, (flying, panel.waited)
    job = NS(fatal="", cancelled=False, move=True, results=[NS(status="verified"), NS(status="skipped")])
    assert common.outcome(job, 1, [])[2] == "warning" and "skipped" in common.outcome(job, 1, [])[1], "a skipped file must keep the window open"
    job.results.pop()
    assert common.outcome(job, 1, [])[2] == "success"
    for done, results in ((0, []), (2, [NS(status="verified"), NS(status="ok")])):  # nothing, or not everything, was checked: never "verified"
        assert "verified" not in common.outcome(NS(fatal="", cancelled=False, move=False, results=results), done, [])[1], results
    app, failures, opened = ui_gtk.App(), [], []

    def probe():
        panel = app.get_active_window()
        if not (panel and panel.job and panel.job.finished and not panel.active) and time.monotonic() < deadline:
            return True  # the copy is still running (slow machine): look again
        failures.extend(problems(panel))
        if opened and "counted" not in went:
            failures.append("the countdown before the power action does not run out")
        if opened and not (panel.sums_row.get_visible() and "file.sha256" in panel.sums_label.get_label() and os.path.exists(f"{tmp}/dst/file.sha256")):
            failures.append("the checksum file is not written or not shown")
        if opened and not panel.sweep.get_visible():  # the thread that looks for them has had a round of 250 ms by now
            failures.append("the leftover of an earlier job in the target folder is not offered for deletion")
        if not opened:
            before = [d.get_label() for _item, _num, d in panel.steps]
            time.sleep(1.2)
            panel.show_step(1)
            if [d.get_label() for _item, _num, d in panel.steps] != before:
                failures.append(f"the times of the steps grow after the job is done: {before} -> {[d.get_label() for _item, _num, d in panel.steps]}")
            opened.append(True)
            panel.show_step(2)
            return True  # the next round checks the window with the read graph
        try:
            app.show_settings()  # the window with the shortcut rows must build
            i18n, ui_settings, cut, before = sys.modules["i18n"], sys.modules["ui_settings"], [], sys.modules["i18n"]._current
            try:
                for lang in dict.fromkeys(["en", *i18n.languages()]):  # the tab names are written out in full in every language
                    i18n.set_language(lang)
                    window = ui_settings.Settings(app)
                    window.present()
                    end = time.monotonic() + 0.3
                    while time.monotonic() < end:
                        GLib.MainContext.default().iteration(False)
                    switcher = next(w for w in descendants(window) if isinstance(w, Adw.ViewSwitcher))
                    if switcher.get_width() < switcher.measure(Gtk.Orientation.HORIZONTAL, -1)[1]:
                        cut.append(lang)
                    window.destroy()
            finally:
                i18n.set_language(before)
            if cut:
                failures.append(f"tab names are cut off in {cut}")
            dialogs.warning_dialog(app.settings, "t", "x", "no", "yes", print)  # asked at once, as after a new language: no width yet
            width = app.settings.get_visible_dialog().get_extra_child().get_size_request()[0]
            if not 0 < width <= 480:
                failures.append(f"a question on a window not drawn yet asks for a width of {width}")
            win = app.settings  # the speed test opens a window with a bar per checksum and changes no setting
            saved = dict(desktop.load_config())
            win.speed_button.emit("clicked")  # the real way: a thread measures, the window opens when done
            end = time.monotonic() + 15
            while not win.speed_button.get_sensitive() and time.monotonic() < end:
                GLib.MainContext.default().iteration(False)
                time.sleep(0.01)
            shown = win.get_visible_dialog()
            if shown is None or desktop.load_config() != saved:
                failures.append("the speed test shows no window, or changes a setting")
            win.show_speeds({"crc32": 3000e6, "sha256": 1100e6, "sha512": 500e6, "sha1": 1200e6, "md5": 500e6})  # every kind of remark
            win.show_speeds({"crc32": 900e6, "sha256": 300e6, "sha512": 600e6, "sha1": 100e6, "md5": 100e6})
        except Exception as e:  # noqa: BLE001 - whatever breaks it is a failure of this test
            failures.append(f"settings window: {e!r}")
        try:
            failures.extend(waiting_problems(app, tmp))
        except Exception as e:  # noqa: BLE001
            failures.append(f"waiting job: {e!r}")
        app.quit()
        return False

    deadline = time.monotonic() + 30
    GLib.timeout_add(250, probe)
    Path(tmp, "dst").mkdir(exist_ok=True)
    Path(tmp, "dst", "old.bin.trustpart").write_bytes(b"left by a crash")
    app.run(["trustcopy", "copy", "--to", f"{tmp}/dst", f"{tmp}/file"])
    if failures:
        sys.exit("; ".join(failures))


def waiting_problems(app, tmp):
    """A job that has to wait keeps its window (its options can be set meanwhile) and says where it stands and what runs; the card is gone once it runs."""
    import threading
    from gi.repository import GLib
    core, Panel = sys.modules["core"], sys.modules["ui_panel"].Panel
    gate, original = threading.Event(), core.Job.run
    core.Job.run = lambda self: (gate.wait(), original(self))[1]  # the first job stands still until the gate opens
    found = []

    def pump(seconds, until=lambda: False):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not until():
            GLib.MainContext.default().iteration(False)
            time.sleep(0.01)
    try:
        panels = []
        for n in (1, 2, 3):
            Path(tmp, f"q{n}").mkdir()
            panel = Panel(app)
            panel.present()
            panel.begin("copy", [f"{tmp}/file"], f"{tmp}/q{n}")
            panels.append(panel)
        pump(1.2)
        if [p.wait_card.get_visible() for p in panels] != [False, True, True]:
            found.append(f"the waiting cards are wrong: {[p.wait_card.get_visible() for p in panels]}")
        elif not panels[1].wait_text.get_text().startswith("Place 2 of 3") or not panels[2].wait_text.get_text().startswith("Place 3 of 3") or "Copying to" not in panels[1].wait_text.get_text():
            found.append(f"a waiting job does not say its place and what runs: {panels[1].wait_text.get_text()!r}")
        if any(p.status.get_visible() for p in panels[1:]):
            found.append("a waiting job still shows the line of the time before it was queued")
        panels[2].close()  # the user closes a window that waits: it leaves the queue, its timer stops, the places move up
        pump(0.2)
        if panels[2] in app.waiting or panels[2].wait_timer or not panels[1].wait_text.get_text().startswith("Place 2 of 2"):
            found.append(f"a closed waiting job stays in the queue or keeps its timer: {panels[1].wait_text.get_text()!r}")
        gate.set()
        pump(20, lambda: app.running is None and not any(p.wait_card.get_visible() for p in panels[:2]))
        if any(p.wait_card.get_visible() for p in panels[:2]) or not all(Path(tmp, f"q{n}", "file").exists() for n in (1, 2)) or Path(tmp, "q3", "file").exists():
            found.append("the queue did not run the jobs left, ran the closed one, or a card stayed")
    finally:
        core.Job.run = original
        gate.set()
    return found


def main():
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "dst").mkdir()
        Path(tmp, "file").write_bytes(b"x" * 100_000)
        Path(tmp, "runtime").mkdir(mode=0o700)
        Path(tmp, "config", "trustcopy").mkdir(parents=True)
        Path(tmp, "config", "trustcopy", "settings.json").write_text('{"sums": true}')  # the real job below writes a checksum file
        # The test must stay off the desk of whoever runs it. X11 only: with WAYLAND_DISPLAY set GTK opens its windows on the real screen, not
        # in Xvfb. No session bus and a runtime folder of its own: on the real ones the test would send a notification, join a TrustCopy that
        # runs, and a portal started for it has unmounted the Flatpak document folder (/run/user/<id>/doc) before.
        env = {**os.environ, "HOME": tmp, "XDG_CONFIG_HOME": f"{tmp}/config", "GSETTINGS_BACKEND": "memory", "GTK_A11Y": "none",
               "GDK_BACKEND": "x11", "XDG_RUNTIME_DIR": f"{tmp}/runtime", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent",
               "GTK_USE_PORTAL": "0", "GDK_DEBUG": "no-portals"}
        env.pop("WAYLAND_DISPLAY", None)
        result = subprocess.run([sys.executable, __file__, tmp], env=env)  # the settings are read at import: a process of its own
        if result.returncode:
            sys.exit(result.returncode)
    print("GTK smoke test ok")


if __name__ == "__main__":
    child(sys.argv[1]) if len(sys.argv) > 1 else main()
