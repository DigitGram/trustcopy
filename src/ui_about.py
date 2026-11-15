# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DigitGram
"""Artwork of the About dialog. Code-Cat: the pixel cat behind the easter egg (five quick clicks on the logo).

This file is the same in TrustCopy and HashTally. It holds the cat, her drawing and her movement, the stage (a popover over the logo)
and one scene both apps share, "check". What she does is a scene, a function scene(stage) -> (draw(cr, t), duration in seconds); the
scenes of each app are in ui_about_art.py."""
import math
import random
import time

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

from i18n import _

P = 4  # screen pixels per sprite pixel (files, folders, ...)
CP = 3  # ... and per cat pixel
COLORS = {}  # the colours of the sprites a scene draws: letter -> (r, g, b); scenes add theirs
OUTLINE, GREY, STRIPE, WHITE, CHEEK, ORANGE = (.1, .1, .15), (.62, .62, .68), (.36, .36, .42), (1, 1, 1), (1, .55, .65), (.9, .6, .1)
CAT_W, CAT_H = 24 * CP, 16 * CP


def lerp(a, b, f):
    return a + (b - a) * f


def clamp(v):
    return max(0.0, min(1.0, v))


def jump(f, a, b, lift):
    """Position at f (0..1) of a hop from a to b that rises `lift` pixels in the middle."""
    return lerp(a[0], b[0], f), lerp(a[1], b[1], f) - lift * 4 * f * (1 - f)


def rects(cr, x, y, flip, scale, color, *boxes):
    """Fill boxes (x, y, w, h in cat pixels); flip mirrors them inside the 24 wide cat."""
    cr.set_source_rgb(*color)
    for bx, by, bw, bh in boxes:
        if flip:
            bx = 24 - bx - bw
        cr.rectangle(x + bx * scale, y + by * scale, bw * scale + 0.5, bh * scale + 0.5)
        cr.fill()


def nyan(cr, x, y, flip=False, t=0.0, eating=False, run=False, alert=False, sx=1.0, sy=1.0, air=False, rot=0.0, swipe=False, carry=None, sleep=False):
    """The cat: a grey tabby with white paws and muzzle, a big cute head (big eyes, blush, pink ears). Faces right unless flipped.
    run: trotting (diagonal leg pairs, bobbing body, speed lines); air: legs stretched out; alert: exclamation marks;
    sx, sy: squash and stretch around the paws; rot: nose up in radians; swipe: a paw strikes at the file; carry: (art, scale) of a sprite in her mouth; sleep: eyes shut."""
    f, k = flip, CP
    cr.save()
    cr.translate(x + CAT_W / 2, y + CAT_H)
    cr.rotate(rot if flip else -rot)
    cr.scale(sx, sy)
    cr.translate(-x - CAT_W / 2, -y - CAT_H)
    ph = t * 17 if run and not air else 0.0
    bob = 1 if run and not air and math.sin(2 * ph) > 0.2 else 0  # the body rises between the footfalls
    yb = y - bob * k
    yh = yb + (2 * k if eating else 0)  # head dips when it bites
    if run:
        for n, ly in enumerate((6, 9, 12)):
            off = (int(t / 0.08) + n) % 3
            rects(cr, x, y, f, k, GREY, (-9 - off, ly, 5, 1))
    for r in range(9):  # tail: a wave runs up from its root
        dx = round((2.0 if run else 1.3) * math.sin(t * 6 + r * 0.55))
        rects(cr, x, yb, f, k, OUTLINE, (dx, 2 + r, 3, 1))
        rects(cr, x, yb, f, k, STRIPE if r in (2, 5) else GREY, (1 + dx, 2 + r, 1, 1))
    for i, dx in enumerate((4, 8, 14, 18)):  # legs with white paws; opposite corners move together
        p = ph + (math.pi if i in (1, 2) else 0)
        lift = round(max(0.0, math.sin(p)) * 2) if run else 0
        shift = (1 if dx > 10 else -1) if air else (round(math.cos(p) * 1.4) if run else 0)
        rects(cr, x, y, f, k, OUTLINE, (dx - 1 + shift, 11 + lift, 4, 5 - lift))
        rects(cr, x, y, f, k, GREY, (dx + shift, 11 + lift, 2, 2))
        rects(cr, x, y, f, k, WHITE, (dx + shift, 13 + lift, 2, 2 - lift))
    rects(cr, x, yb, f, k, OUTLINE, (3, 5, 14, 9), (2, 6, 16, 7))  # body
    rects(cr, x, yb, f, k, GREY, (4, 6, 12, 7), (3, 7, 14, 5))
    rects(cr, x, yb, f, k, STRIPE, (6, 6, 1, 2), (9, 6, 1, 3), (12, 6, 1, 2))
    rects(cr, x, yb, f, k, WHITE, (5, 11, 8, 2))  # belly
    if carry:  # held in the mouth: drawn first, so the head covers the end that is gripped
        art, cs = carry
        sprite(cr, art, x + (3 * k - len(art[0]) * P * cs if f else 21 * k), yh + 6 * k, cs)
    rects(cr, x, yh, f, k, OUTLINE, (12, 4, 11, 11), (11, 5, 13, 9), (12, 2, 3, 3), (20, 2, 3, 3))  # head with ears
    rects(cr, x, yh, f, k, GREY, (13, 5, 9, 9), (12, 6, 11, 7), (13, 3, 1, 2), (21, 3, 1, 2))
    rects(cr, x, yh, f, k, CHEEK, (14, 3, 1, 2), (20, 3, 1, 2))  # inner ears
    rects(cr, x, yh, f, k, STRIPE, (16, 5, 1, 2), (18, 5, 1, 2), (20, 5, 1, 1))  # forehead stripes
    rects(cr, x, yh, f, k, WHITE, (15, 11, 5, 3), (16, 10, 3, 1))  # muzzle
    rects(cr, x, yh, f, k, CHEEK, (12, 10, 2, 2), (21, 10, 2, 2))  # blush
    if sleep or (int(t * 10) % 27 == 0 and not alert):  # asleep, or a blink
        rects(cr, x, yh, f, k, OUTLINE, (14, 9, 2, 1), (20, 9, 2, 1))
    else:
        rects(cr, x, yh, f, k, OUTLINE, (14, 7, 2, 3), (20, 7, 2, 3))  # eyes
        rects(cr, x, yh, f, k, WHITE, (14, 7, 1, 1), (20, 7, 1, 1), (15, 9, 1, 1), (21, 9, 1, 1))  # sparkles
    rects(cr, x, yh, f, k, CHEEK, (17, 10, 2, 1))  # nose
    if eating or alert:
        rects(cr, x, yh, f, k, OUTLINE, (16, 11, 4, 3))  # open mouth with a tongue
        rects(cr, x, yh, f, k, CHEEK, (17, 13, 2, 1))
    else:
        rects(cr, x, yh, f, k, OUTLINE, (16, 11, 1, 1), (17, 12, 2, 1), (19, 11, 1, 1))
    if swipe:  # a white paw claws at the paper in front of the face
        rects(cr, x, yh, f, k, OUTLINE, (21, 9, 5, 4))
        rects(cr, x, yh, f, k, WHITE, (22, 10, 3, 2))
    if alert:
        rects(cr, x, yh, f, k, ORANGE, (15, -4, 1, 2), (15, -1, 1, 1), (21, -4, 1, 2), (21, -1, 1, 1))
    cr.restore()


def shadow(cr, x, y):
    """Soft shadow of a cat standing on the ground (x, y: the cat's top left)."""
    cr.save()
    cr.translate(x + CAT_W / 2, y + CAT_H)
    cr.scale(CAT_W / 2, 5)
    cr.arc(0, 0, 1, 0, 2 * math.pi)
    cr.restore()
    cr.set_source_rgba(0, 0, 0, 0.18)
    cr.fill()


def sprite(cr, art, x, y, scale=1.0):
    for r, row in enumerate(art):
        for c, ch in enumerate(row):
            if ch in COLORS:
                cr.set_source_rgb(*COLORS[ch])
                cr.rectangle(x + c * P * scale, y + r * P * scale, P * scale + 0.5, P * scale + 0.5)
                cr.fill()


class Stage:
    """Where things are: the popover is centred on the logo, with room above it and a strip of ground below."""

    def __init__(self, size):
        self.size, self.above = size, 56
        self.w, self.h = size + 300, self.above + size + 40
        self.lx, self.ly = (self.w - size) / 2, self.above  # the logo's top left
        self.ground, self.top = self.ly + size, self.ly + 6  # its bottom, and the line she stands on up there
        self.sit_y, self.out_y = self.top - CAT_H, self.ground - CAT_H  # the cat's top when standing on the logo / on the ground


def play(logo, scene):
    """Run `scene` over `logo` (a Gtk.Image); ignores further calls while it runs."""
    if getattr(logo, "cat_busy", False):
        return
    logo.cat_busy = True
    stage = Stage(logo.get_pixel_size() if logo.get_pixel_size() > 0 else 128)
    draw, duration = scene(stage)
    start = time.monotonic()

    def tick():
        area.queue_draw()
        if time.monotonic() - start < duration:
            return GLib.SOURCE_CONTINUE
        pop.popdown()
        pop.unparent()
        logo.cat_busy = False
        return GLib.SOURCE_REMOVE

    area = Gtk.DrawingArea(content_width=stage.w, content_height=stage.h)
    area.set_draw_func(lambda a, cr, aw, ah: draw(cr, time.monotonic() - start))
    pop = Gtk.Popover(child=area, has_arrow=False, autohide=False, can_target=False, position=Gtk.PositionType.BOTTOM)
    pop.add_css_class("k-cat")
    pop.set_parent(logo)
    pop.set_offset(0, -stage.above - stage.size)  # BOTTOM puts the popover under the logo: lift it so the logo sits in its middle
    pop.popup()
    GLib.timeout_add(16, tick)


def hint(logo, text=None):
    """A small note over the logo for a moment (None: take the old one away); a new note replaces the one before."""
    old = getattr(logo, "cat_hint", None)
    logo.cat_hint = None
    if old:
        old.popdown()
        old.unparent()
    if not text:
        return
    pop = Gtk.Popover(child=Gtk.Label(label=text), autohide=False, can_target=False, position=Gtk.PositionType.TOP)
    pop.set_parent(logo)
    pop.popup()
    logo.cat_hint = pop

    def gone():
        if logo.cat_hint is pop:
            hint(logo)
        return GLib.SOURCE_REMOVE
    GLib.timeout_add(1200, gone)


def attach(dialog, scenes):
    """Five quick clicks on the logo in an Adw.AboutDialog play one of `scenes`, never the same twice in a row; the cat then joins the credits."""
    def first_image(widget):
        if isinstance(widget, Gtk.Image):
            return widget
        child = widget.get_first_child()
        while child:
            if (found := first_image(child)):
                return found
            child = child.get_next_sibling()

    logo = first_image(dialog)
    if not logo:
        return  # libadwaita changed its layout: no egg, no harm
    unlocked, last = [], []
    clicks, last_click = 0, 0.0   # our own count: GTK's n_press starts over after 0.4 s or when the pointer moves a little

    def clicked(gesture, n_press, *a):
        nonlocal clicks, last_click
        now = time.monotonic()
        clicks = clicks + 1 if now - last_click < 1.5 else 1   # slow clicking (one every 1.5 s at most) counts too
        last_click, n = now, clicks
        if n in (2, 3, 4):  # like Android's developer options: a note counts down the last clicks
            hint(logo, (_("3 more clicks …"), _("2 more clicks …"), _("1 more click …"))[n - 2])
        if n == 5:
            clicks = 0
            hint(logo)
            scene = random.choice([sc for sc in scenes if sc not in last] or scenes)
            last[:] = [scene]
            play(logo, scene)
            if not unlocked:
                unlocked.append(1)
                dialog.add_credit_section(_("Mascot"), ["Code-Cat"])
    click = Gtk.GestureClick()
    click.connect("pressed", clicked)
    logo.add_controller(click)


# ---------------------------------------------------------------------------------------------------------------------------------
COLORS.update({"b": (0.55, 0.57, 0.62), "w": (0.97, 0.97, 0.97), "l": (0.35, 0.55, 0.9), "r": (0.9, 0.25, 0.25), "g": (0.15, 0.65, 0.3)})
FILE = ("bbbbbbb...", "bwwwwwbb..", "bwlllwbwb.", "bwwwwwbbbb", "bwlllllllb", "bwwwwwwwwb", "bwlllllwwb", "bwwwwwwwwb", "bwlllllwwb", "bwwwwwwwwb", "bwlllwwwwb", "bbbbbbbbbb")
BAD = tuple(row.replace("lllllww", "llrllww") if i == 6 else row for i, row in enumerate(FILE))  # the same file with one pixel off
CHECK = ("......g", ".....gg", "g...gg.", "gg.gg..", ".ggg...", "..g....")
CROSS = ("rr...rr", "rrr.rrr", ".rrrrr.", "..rrr..", ".rrrrr.", "rrr.rrr", "rr...rr")  # the red X that says why she throws a file out
DS = 0.7  # the files are drawn a bit smaller than the cat's paper, so two fit beside the cat
DOC_W, DOC_H = len(FILE[0]) * P * DS, len(FILE) * P * DS
# The timeline, in seconds.
T_OUT, T_JUMP = 1.3, 1.7  # walked out, jumps up
T_LAND = T_JUMP + 0.5
T_PAIR1, T_PAIR1_GONE = 0.3, 4.2
T_PAIR2, T_PAIR2_GONE = 4.6, 7.0
LOOKS = ((T_LAND, True), (2.6, False), (2.9, True), (3.2, True), (3.5, False), (5.3, True), (5.6, False))  # (from, faces left)
STAMPS = ((3.2, 3.5), (3.5, 3.8), (6.4, 6.7))  # paw strikes
T_TICK_A, T_TICK_B, T_HOP = 3.35, 3.65, 3.9
T_ALERT, T_SWAT = 5.9, 6.5
T_LEAP, T_END = 7.3, 8.1


def check(st):
    size, lx, top, w, h, sit_y, out_y = st.size, st.lx, st.top, st.w, st.h, st.sit_y, st.out_y
    sit_x = lx + (size - CAT_W) / 2
    doc_a, doc_b, doc_y = lx + 6, lx + size - 6 - DOC_W, top - DOC_H

    def doc_top(t, up, down):
        """Where a file's top is: hidden behind the logo before `up`, risen after it, sunk again after `down`."""
        y = lerp(top, doc_y, 1 - (1 - clamp((t - up) / 0.6)) ** 3)
        return lerp(y, top, clamp((t - down) / 0.4) ** 2)

    def doc(cr, x, y, art, tick=None, t=0.0, mark=CHECK):
        """A file standing on the logo's top edge, clipped there so it comes from behind the logo."""
        if y >= top - 1:
            return
        cr.save()
        cr.rectangle(x - 2, 0, DOC_W + 4, top)
        cr.clip()
        sprite(cr, art, x, y, DS)
        if tick is not None and t >= tick:
            s = 0.9 * min(1.15, (t - tick) / 0.12 + 0.3)
            sprite(cr, mark, x + DOC_W / 2 - 3.5 * P * s, y + DOC_H / 2 - 3 * P * s, s)
        cr.restore()

    def papers(cr, t):
        ya = doc_top(t, T_PAIR1, T_PAIR1_GONE)
        doc(cr, doc_a, ya, FILE, T_TICK_A, t)
        doc(cr, doc_b, ya, FILE, T_TICK_B, t)
        yc = doc_top(t, T_PAIR2, T_PAIR2_GONE)
        doc(cr, doc_a, yc, FILE)
        if t < T_SWAT:
            doc(cr, doc_b, yc, BAD)
        elif t < T_SWAT + 1.0:  # the odd one out is batted off the logo, spinning
            age = t - T_SWAT
            cr.save()
            cr.translate(doc_b + DOC_W / 2 + 150 * age, doc_y + DOC_H / 2 - 230 * age + 600 * age * age)
            cr.rotate(9 * age)
            sprite(cr, BAD, -DOC_W / 2, -DOC_H / 2, DS)
            sprite(cr, CROSS, -3.5 * P * 0.9, -3 * P * 0.9, 0.9)  # the X flies with it
            cr.restore()

    def faces_left(t):
        return [left for start_t, left in LOOKS if start_t <= t][-1]

    def draw(cr, t):
        papers(cr, t)
        if t < T_OUT:  # out from behind the logo: only what is right of its edge shows
            cr.rectangle(lx + size, 0, w, h)
            cr.clip()
            x = lerp(lx + size - CAT_W + 20, lx + size + 28, t / T_OUT)
            shadow(cr, x, out_y)
            nyan(cr, x, out_y, t=t, run=True)
            return
        if t < T_JUMP:  # turns round, spots the files, winds up
            shadow(cr, lx + size + 28, out_y)
            c = clamp((t - T_JUMP + 0.25) / 0.25) * 0.3
            nyan(cr, lx + size + 28, out_y, flip=True, t=t, alert=True, sx=1 + c * 0.6, sy=1 - c, rot=math.sin(t * 45) * 0.06 * c / 0.3)
            return
        if t < T_LAND:
            f = (t - T_JUMP) / 0.5
            x, y = jump(f, (lx + size + 28, out_y), (sit_x, sit_y), 24)
            nyan(cr, x, y, flip=True, t=t, run=True, air=True, sx=1 - 0.15 * math.sin(math.pi * f), sy=1 + 0.25 * math.sin(math.pi * f), rot=0.4 * math.cos(math.pi * f))
            return
        if t >= T_LEAP:
            f = clamp((t - T_LEAP) / 0.6)
            x, y = jump(f, (sit_x, sit_y), (lx + size + 160, out_y), 30)
            nyan(cr, x, y, t=t, run=True, air=f <= 0.9, sx=1 - 0.12 * math.sin(math.pi * f), sy=1 + 0.25 * math.sin(math.pi * f), rot=0.45 * math.cos(math.pi * f))
            return
        tau = t - T_LAND  # sitting between the files: landing squash, hop of joy, winding up for the leap
        land = 0.3 * math.exp(-9 * tau) * math.cos(22 * tau)
        crouch = clamp((t - T_LEAP + 0.3) / 0.3) ** 2
        squash, lift = land + crouch * 0.32, 0.0
        if T_HOP <= t < T_HOP + 0.4:
            f = (t - T_HOP) / 0.4
            lift, squash = 24 * 4 * f * (1 - f), -0.18 * math.sin(math.pi * f)
        pawing = any(a <= t < b for a, b in STAMPS) and int(t / 0.12) % 2 == 0
        nyan(cr, sit_x, sit_y - lift, flip=faces_left(t) and crouch == 0, t=t, alert=T_ALERT <= t < T_SWAT, swipe=pawing,
             sx=1 + squash * 0.6, sy=1 - squash, rot=-0.05 * crouch)
        if T_ALERT <= t < T_SWAT:  # the red X on the odd file, in front of her so her head does not hide it
            k = 0.8 * min(1.15, (t - T_ALERT) / 0.12 + 0.3)
            sprite(cr, CROSS, doc_b + DOC_W * 0.65 - 3.5 * P * k, doc_y + DOC_H / 2 - 3 * P * k, k)

    return draw, T_END
