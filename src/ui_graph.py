# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DigitGram
"""The speed graph's drawing and speed maths: pure cairo, no widgets."""
import math
import re

from gi.repository import GLib

from ui_common import fmt_duration


GREEN, RED, BLUE = (0.15, 0.64, 0.41), (0.88, 0.11, 0.14), (0.21, 0.52, 0.89)  # writing, a failed file, reading back
SMOOTH = 0.1  # weight of the newest 250 ms in the shown speed; low, so graph and ETA stay calm


def smoothed(old, instant, n):
    """The shown speed with the newest value `instant` as the n-th: the first values weigh 1/n, so the curve starts at the real speed
    and not at zero; from the 10th on it is the calm average that SMOOTH says."""
    return old + (instant - old) * max(SMOOTH, 1 / max(n, 1))


HALF = 1.5  # seconds each side that a point looks at its neighbours: the curve lags about a second, a job of minutes does not show it


def soften(points, half=HALF):
    """Each point as the mean of the points within `half` seconds, the nearer the heavier. The trailing mean of Panel.speeds has hard
    edges, every burst that enters or leaves its window is a step, and a stick that takes data in bursts frays the curve; this is the second,
    soft pass. The times stay; a point near the end only has the points before it."""
    out, lo, hi = [], 0, 0
    for t, _v in points:
        while points[lo][0] <= t - half:
            lo += 1
        while hi < len(points) and points[hi][0] < t + half:
            hi += 1
        near = [(1 - abs(t2 - t) / half, v2) for t2, v2 in points[lo:hi]]
        out.append((t, sum(w * v2 for w, v2 in near) / sum(w for w, _v2 in near)))
    return out


def curve(cr, pts):
    """Extends the path through pts as a monotone cubic (Fritsch-Carlson): it never overshoots, and a falling or rising
    run stays a slope; a horizontal tangent at every point (as the System Monitor has) would turn it into steps."""
    d = [(y1 - y0) / (x1 - x0) if x1 > x0 else 0 for (x0, y0), (x1, y1) in zip(pts, pts[1:])]  # slope per segment
    m = [d[0], *(0 if a * b <= 0 else 2 * a * b / (a + b) for a, b in zip(d, d[1:])), d[-1]]  # slope per point; 0 on a peak or a dip
    for (x0, y0), (x1, y1), m0, m1 in zip(pts, pts[1:], m, m[1:]):
        k = (x1 - x0) / 3
        cr.curve_to(x0 + k, y0 + m0 * k, x1 - k, y1 - m1 * k, x1, y1)


TITLE = 30  # height of the strip with the graph's name above it


def draw_title(cr, fg, h, text):
    """The graph's name in the strip above it. Moves the origin below the strip and returns the height left for the graph."""
    cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.95)
    cr.select_font_face("", 0, 1)  # bold
    cr.set_font_size(13)
    cr.move_to(14, 21)
    cr.show_text(text)
    cr.select_font_face("", 0, 0)
    cr.translate(0, TITLE)
    return h - TITLE


def draw_grid(cr, fg, w, h):
    """The faint grid behind the curve: 3 horizontal and 9 vertical lines."""
    cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.12)
    cr.set_line_width(1)
    for i in range(1, 4):
        cr.move_to(0, h * i / 4)
        cr.line_to(w, h * i / 4)
    for i in range(1, 10):
        cr.move_to(w * i / 10, 0)
        cr.line_to(w * i / 10, h)
    cr.stroke()


def draw_curve(cr, pts, h, color=GREEN):
    """The speed curve through pts: an area down to the bottom edge, then the line on top."""
    cr.move_to(pts[0][0], h)
    cr.line_to(*pts[0])
    curve(cr, pts)
    cr.line_to(pts[-1][0], h)
    cr.close_path()
    cr.set_source_rgba(*color, 0.3)
    cr.fill()
    cr.move_to(*pts[0])
    curve(cr, pts)
    cr.set_source_rgba(*color, 1)
    cr.set_line_width(2)
    cr.stroke()


def plot(cr, fg, w, h, samples, color, line, marks=(), min_span=0.05):
    """A whole speed graph into the grid already drawn: curve, failure marks and both scales; `line` (bytes/s, the last speed or the average) sizes the grid.
    A burst into the cache must not flatten the rest: above 3 times the mean the curve runs off the top."""
    step = nice_step(min(max(line, *(v for _t, v in samples)), 3 * sum(v for _t, v in samples) / len(samples)))
    top = step * 4  # the grid has 4 rows: every line stands for a round speed
    span = max(samples[-1][0], min_span)  # a finished job fills the width, however short

    def y(v):
        return h - 2 - min(v, top) / top * (h - 6)

    draw_curve(cr, [(t / span * w, y(v)) for t, v in samples], h, color)
    cr.set_source_rgba(*RED, 0.9)  # a file failed here
    for t in marks:
        cr.move_to(min(t / span, 1) * w, 0)
        cr.line_to(min(t / span, 1) * w, h)
    cr.stroke()
    draw_time_scale(cr, fg, w, h, span)
    draw_speed_scale(cr, fg, h, step)


def nice_step(peak):
    """The grid step, a round speed in bytes/s, that puts `peak` (plus 5 % room) within the 4 rows of the graph."""
    need, k = max(peak, 1) * 1.05, 10 ** math.floor(math.log10(max(peak, 1) * 1.05 / 4))
    return next(m * k for m in (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10) if m * k * 4 >= need)


def show_label(cr, fg, text):
    """The text at the current point, with a thin halo in the opposite colour: a curve running through a scale label must not swallow it."""
    cr.text_path(text)
    cr.set_source_rgba(1 - fg.red, 1 - fg.green, 1 - fg.blue, 0.7)
    cr.set_line_width(3)
    cr.stroke_preserve()
    cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.75)
    cr.fill()


def draw_speed_scale(cr, fg, h, step):
    """The speeds the 3 horizontal grid lines stand for, small at the left edge above each line."""
    cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
    cr.set_font_size(10)
    for i in range(1, 4):
        cr.move_to(10, h * i / 4 - 4)
        show_label(cr, fg, re.sub(r"[.,]0(\s)", r"\1", GLib.format_size(int(step * (4 - i)))) + "/s")


def draw_time_scale(cr, fg, w, h, span):
    """The time scale under the curve: 0 s at the left, the elapsed time at the right."""
    cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
    cr.set_font_size(10)
    cr.move_to(10, h - 7)  # clear of the card's rounded corners
    show_label(cr, fg, "0 s")
    end = fmt_duration(span)
    cr.move_to(w - cr.text_extents(end).width - 10, h - 7)
    show_label(cr, fg, end)


def draw_badge(cr, fg, w, h, lines, alpha=0.85):
    """Info text in a rounded frame in the middle of the graph, a line of text per entry of `lines`."""
    cr.set_font_size(18)
    bw, bh = max(cr.text_extents(t).width for t in lines) + 52, 18 + 26 * len(lines)
    x, y, r = (w - bw) / 2, (h - bh) / 2, min(bh / 2, 22)
    cr.new_sub_path()
    for cx, cy, start in ((x + bw - r, y + r, -90), (x + bw - r, y + bh - r, 0), (x + r, y + bh - r, 90), (x + r, y + r, 180)):
        cr.arc(cx, cy, r, math.radians(start), math.radians(start + 90))
    cr.close_path()
    light = fg.red * 0.299 + fg.green * 0.587 + fg.blue * 0.114 > 0.5  # light text: a dark theme
    cr.set_source_rgba(*((0.1, 0.1, 0.1, 0.55) if light else (1, 1, 1, 0.8)))  # a ground against the text colour: it reads on the green area as well as outside it
    cr.fill_preserve()
    cr.set_source_rgba(fg.red, fg.green, fg.blue, alpha)
    cr.set_line_width(1.5)
    cr.stroke()
    for i, text in enumerate(lines):
        cr.move_to(x + 26 + (bw - 52 - cr.text_extents(text).width) / 2, y + 29 + 26 * i)  # centred in the frame
        cr.show_text(text)
