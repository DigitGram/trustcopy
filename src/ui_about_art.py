# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DigitGram
"""TrustCopy's Code-Cat scenes (SCENES; one is picked at random): the courier, the check (in ui_about.py, shared with HashTally) and "mine"."""
import math

from ui_about import CAT_W, CHECK, COLORS, CP, FILE, P, check, clamp, jump, lerp, nyan, shadow, sprite

COLORS.update({"b": (0.55, 0.57, 0.62), "w": (0.97, 0.97, 0.97), "l": (0.35, 0.55, 0.9), "g": (0.15, 0.65, 0.3),
               "o": (0.62, 0.42, 0.12), "h": (0.99, 0.88, 0.5), "y": (0.96, 0.74, 0.25)})
FOLDER = ("oooooo........", "ohhhhhoooooooo", "ohhhhhhhhhhhho", "ohyyyyyyyyyyyo", "ohyyyyyyyyyyyo", "ohyyyyyyyyyyyo", "ohyyyyyyyyyyyo", "ohyyyyyyyyyyyo", "oooooooooooooo")
FS, CS, KS = 0.8, 0.6, 0.8  # scale of the file on the logo, of the copy in her mouth, of the folder
FILE_W, FILE_H = len(FILE[0]) * P * FS, len(FILE) * P * FS
COPY_W, COPY_H = len(FILE[0]) * P * CS, len(FILE) * P * CS
FOL_W, FOL_H = len(FOLDER[0]) * P * KS, len(FOLDER) * P * KS

# The timeline, in seconds.
T_FILE, T_FOLDER = 0.3, 0.6
T_OUT, T_JUMP, T_LAND = 1.3, 1.7, 2.2
T_TAP, T_COPY = 2.3, 2.5  # paw taps from T_TAP; the copy hops out at T_COPY and is in her mouth 0.3 s later
T_TURN, T_TROT, T_DOWN, T_GROUND = 2.8, 2.95, 3.5, 4.05  # turned round, trotting along the logo, leaping down, landed
T_DROP, T_WOBBLE, T_TICK, T_JOY = 4.3, 4.7, 4.95, 5.0
T_LEAP, T_FSINK, T_END = 5.6, 5.8, 6.7

# ---------------------------------------------------------------------------------------------------------------------------------
# Courier: a file rises behind the logo and a folder pops up on the ground at the right. The cat lands on the logo, taps the file (a copy
# pops out, the original stays), carries the copy to the folder, drops it in, the folder gets a tick, she leaves.


def courier(st):
    size, lx, ground, top, w, h, sit_y, out_y = st.size, st.lx, st.ground, st.top, st.w, st.h, st.sit_y, st.out_y
    file_x, file_y = lx + 2, top - FILE_H
    x_a, x_edge, x_e = lx + 22, lx + size - CAT_W + 22, lx + size + 30  # on the logo by the file, at its right edge, on the ground
    fol_x, fol_y = lx + size + 100, ground - FOL_H

    def file_top(t):
        """The file on the logo: hidden behind it at first, risen, and sunk again at the end."""
        y = lerp(top, file_y, 1 - (1 - clamp((t - T_FILE) / 0.6)) ** 3)
        return lerp(y, top, clamp((t - T_FSINK) / 0.4) ** 2)

    def papers(cr, t):
        y = file_top(t)
        if y < top - 1:
            cr.save()
            cr.rectangle(file_x - 2, 0, FILE_W + 4, top)  # clipped at the logo's top edge: it comes from behind
            cr.clip()
            sprite(cr, FILE, file_x, y, FS)
            cr.restore()
        if T_COPY <= t < T_COPY + 0.3:  # the copy hops out and into her mouth
            f = (t - T_COPY) / 0.3
            x, y = jump(f, (file_x + FILE_W / 2 - COPY_W / 2, file_y), (x_a - 15, sit_y + 6 * CP), 24)
            sprite(cr, FILE, x, y, CS)
        if T_DROP <= t < T_DROP + 0.4:  # the copy falls into the folder; the folder is drawn over it afterwards
            f = (t - T_DROP) / 0.4
            x, y = jump(f, (x_e + CAT_W + 15 - COPY_W, out_y + 6 * CP), (fol_x + (FOL_W - COPY_W) / 2, fol_y + 4), 14)
            sprite(cr, FILE, x, y, CS)

    def folder(cr, t):
        p = clamp((t - T_FOLDER) / 0.3)  # pops up from the ground, a little overshoot
        if p <= 0:  # not there yet (a scale of exactly 0 would be an invalid matrix)
            return
        sx, sy = 1 - 0.1 * math.sin(math.pi * p), (1 - (1 - p) ** 3) * (1 + 0.25 * math.sin(math.pi * p))
        tau = t - T_WOBBLE  # a shake when the copy has landed in it
        if tau > 0:
            sy *= 1 + 0.15 * math.exp(-9 * tau) * math.cos(30 * tau)
        cr.save()
        cr.rectangle(0, 0, w, ground)  # sinks into the ground at the end
        cr.clip()
        cr.translate(fol_x + FOL_W / 2, ground + FOL_H * clamp((t - T_FSINK - 0.1) / 0.4) ** 2)
        cr.scale(sx, sy)
        sprite(cr, FOLDER, -FOL_W / 2, -FOL_H, KS)
        if t >= T_TICK:
            s = 0.9 * min(1.15, (t - T_TICK) / 0.12 + 0.3)
            sprite(cr, CHECK, -3.5 * P * s, -FOL_H * 0.55 - 3 * P * s, s)
        cr.restore()

    def draw(cr, t):
        papers(cr, t)
        folder(cr, t)
        if t < T_OUT:  # out from behind the logo: only what is right of its edge shows
            cr.rectangle(lx + size, 0, w, h)
            cr.clip()
            x = lerp(lx + size - CAT_W + 20, lx + size + 28, t / T_OUT)
            shadow(cr, x, out_y)
            nyan(cr, x, out_y, t=t, run=True)
        elif t < T_JUMP:  # turns round, spots the file, winds up
            shadow(cr, lx + size + 28, out_y)
            c = clamp((t - T_JUMP + 0.25) / 0.25) * 0.3
            nyan(cr, lx + size + 28, out_y, flip=True, t=t, alert=True, sx=1 + c * 0.6, sy=1 - c, rot=math.sin(t * 45) * 0.06 * c / 0.3)
        elif t < T_LAND:
            f = (t - T_JUMP) / 0.5
            x, y = jump(f, (lx + size + 28, out_y), (x_a, sit_y), 24)
            nyan(cr, x, y, flip=True, t=t, run=True, air=True, sx=1 - 0.15 * math.sin(math.pi * f), sy=1 + 0.25 * math.sin(math.pi * f), rot=0.4 * math.cos(math.pi * f))
        elif t < T_TURN:  # on the logo by the file: lands, taps it, takes the copy
            tau = t - T_LAND
            land = 0.3 * math.exp(-9 * tau) * math.cos(22 * tau)
            nyan(cr, x_a, sit_y, flip=True, t=t, swipe=T_TAP <= t < T_COPY + 0.2 and int(t / 0.12) % 2 == 0, carry=(FILE, CS) if t >= T_COPY + 0.3 else None,
                 sx=1 + land * 0.6, sy=1 - land)
        elif t < T_TROT:
            nyan(cr, x_a, sit_y, t=t, carry=(FILE, CS))
        elif t < T_DOWN:  # trots to the edge of the logo
            nyan(cr, lerp(x_a, x_edge, (t - T_TROT) / (T_DOWN - T_TROT)), sit_y, t=t, run=True, carry=(FILE, CS))
        elif t < T_GROUND:  # leaps down to the ground
            f = (t - T_DOWN) / (T_GROUND - T_DOWN)
            x, y = jump(f, (x_edge, sit_y), (x_e, out_y), 20)
            nyan(cr, x, y, t=t, run=True, air=True, carry=(FILE, CS), sx=1 - 0.12 * math.sin(math.pi * f), sy=1 + 0.25 * math.sin(math.pi * f), rot=-0.3 * math.cos(math.pi * f))
        elif t < T_LEAP:  # on the ground by the folder: lands, drops the copy, joy, winds up
            tau = t - T_GROUND
            squash = 0.3 * math.exp(-9 * tau) * math.cos(22 * tau) + clamp((t - T_LEAP + 0.3) / 0.3) ** 2 * 0.32
            lift = 0.0
            if T_JOY <= t < T_JOY + 0.4:
                f = (t - T_JOY) / 0.4
                lift, squash = 24 * 4 * f * (1 - f), -0.18 * math.sin(math.pi * f)
            shadow(cr, x_e, out_y)
            nyan(cr, x_e, out_y - lift, t=t, carry=(FILE, CS) if t < T_DROP else None, eating=T_DROP - 0.05 <= t < T_DROP + 0.2, sx=1 + squash * 0.6, sy=1 - squash)
        else:
            f = clamp((t - T_LEAP) / 0.6)
            x, y = jump(f, (x_e, out_y), (lx + size + 270, out_y), 44)
            nyan(cr, x, y, t=t, run=True, air=f <= 0.9, sx=1 - 0.12 * math.sin(math.pi * f), sy=1 + 0.25 * math.sin(math.pi * f), rot=0.45 * math.cos(math.pi * f))

    return draw, T_END


# ---------------------------------------------------------------------------------------------------------------------------------
# "Mine": a file is on the logo, a second file with the same name arrives and wants to replace it (the app's conflict question).
# The warning sign says there is a conflict. The cat lies down on the first file and will not move: a stop sign answers each push of the
# newcomer, it gives up, and she drifts off, hearts rising from her (she keeps her file).
M_A, M_B, M_OUT, M_JUMP, M_LAND, M_LIE = 0.3, 0.9, 1.3, 1.7, 2.2, 2.45
M_TRIES = (2.8, 3.25, 3.7)  # when the newcomer pushes
M_AWAY, M_SLEEP, M_WAKE, M_LEAP, M_SINK, M_END = 4.1, 4.7, 5.9, 6.2, 6.5, 7.2
COLORS.update({"r": (0.9, 0.25, 0.25), "k": (0.15, 0.15, 0.2), "p": (1.0, 0.45, 0.6)})
WARN = ("....o....", "...oyo...", "..oyyyo..", "..oykyo..", ".oyykyyo.", ".oyykyyo.", "oyyyyyyyo", "oyyykyyyo", "ooooooooo")  # the conflict sign
STOP = ("..rrrrr..", ".rrrrrrr.", "rrrrrrrrr", "rrrrrrrrr", "rwwwwwwwr", "rrrrrrrrr", "rrrrrrrrr", ".rrrrrrr.", "..rrrrr..")  # "no"
HEART = (".pp.pp.", "ppppppp", "ppppppp", ".ppppp.", "..ppp..", "...p...")
M_WARN = (1.5, 2.2)  # the warning sign over the two same-named files


def mine(st):
    size, lx, top, w, sit_y, out_y = st.size, st.lx, st.top, st.w, st.sit_y, st.out_y
    a_x, b_rest, x_c = lx + size / 2 - FILE_W / 2, lx + size - 4 - FILE_W, lx + 22  # the first file, the newcomer's place, where she lies
    y_rest = top - FILE_H

    def b_pos(t):
        """The newcomer slides in from the right, pushes against the cat three times, then trudges off."""
        x = lerp(w, b_rest, 1 - (1 - clamp((t - M_B) / 0.6)) ** 3)
        for t0 in M_TRIES:
            x -= 12 * math.sin(math.pi * clamp((t - t0) / 0.3))
        return x + clamp((t - M_AWAY) / 0.7) ** 2 * (w - b_rest)

    def paper(cr, x, y, clip_top=True):
        cr.save()
        cr.rectangle(x - 2, 0, FILE_W + 4, top if clip_top else st.h)
        cr.clip()
        sprite(cr, FILE, x, y, FS)
        cr.restore()

    def draw(cr, t):
        ya = lerp(top, y_rest, 1 - (1 - clamp((t - M_A) / 0.6)) ** 3)
        ya = lerp(ya, top, clamp((t - M_SINK) / 0.4) ** 2)
        if ya < top - 1:
            paper(cr, a_x, ya)
        if t >= M_B and b_pos(t) < w:
            paper(cr, b_pos(t), y_rest, clip_top=False)
        if M_WARN[0] <= t < M_WARN[1]:  # same file, same place: conflict
            k = 0.55 * min(1.15, (t - M_WARN[0]) / 0.12 + 0.3) * (1 - clamp((t - M_WARN[1] + 0.15) / 0.15))
            sprite(cr, WARN, (a_x + FILE_W + b_rest) / 2 - 4.5 * P * k, 3, k)
        if t < M_OUT:
            cr.rectangle(lx + size, 0, w, st.h)
            cr.clip()
            x = lerp(lx + size - CAT_W + 20, lx + size + 28, t / M_OUT)
            shadow(cr, x, out_y)
            nyan(cr, x, out_y, t=t, run=True)
        elif t < M_JUMP:  # spots the second file: not on her watch
            shadow(cr, lx + size + 28, out_y)
            c = clamp((t - M_JUMP + 0.25) / 0.25) * 0.3
            nyan(cr, lx + size + 28, out_y, flip=True, t=t, alert=True, sx=1 + c * 0.6, sy=1 - c, rot=math.sin(t * 45) * 0.06 * c / 0.3)
        elif t < M_LAND:
            f = (t - M_JUMP) / 0.5
            x, y = jump(f, (lx + size + 28, out_y), (x_c, sit_y), 24)
            nyan(cr, x, y, flip=True, t=t, run=True, air=True, sx=1 - 0.15 * math.sin(math.pi * f), sy=1 + 0.25 * math.sin(math.pi * f), rot=0.4 * math.cos(math.pi * f))
        elif t < M_LEAP:
            tau = t - M_LAND
            land = 0.3 * math.exp(-9 * tau) * math.cos(22 * tau)
            loaf = clamp((t - M_LIE) / 0.25) * (1 - clamp((t - M_WAKE) / 0.2))  # lies down on the file: wide and flat; gets up when she wakes
            crouch = clamp((t - M_LEAP + 0.25) / 0.25) ** 2  # winds up for the leap
            sx, sy = 1 + land * 0.6 + 0.14 * loaf + 0.18 * crouch, 1 - land - 0.34 * loaf - 0.3 * crouch
            hiss = any(t0 + 0.1 <= t < t0 + 0.35 for t0 in M_TRIES)
            asleep = M_SLEEP <= t < M_WAKE - 0.2
            nyan(cr, x_c, sit_y, flip=t < M_LIE, t=t, alert=hiss or M_WAKE - 0.2 <= t < M_WAKE + 0.2, sleep=asleep, sx=sx, sy=sy)
            for t0 in M_TRIES:  # a stop sign over her head at every push
                if t0 + 0.05 <= t < t0 + 0.4:
                    k = 0.75 * min(1.0, (t - t0 - 0.05) / 0.08 + 0.3)
                    sprite(cr, STOP, x_c + CAT_W * 0.6 - 4.5 * P * k, 3, k)
            if asleep:  # hearts drift up from her head
                for i in range(3):
                    f = ((t - M_SLEEP) / 1.2 + i / 3) % 1.0
                    k = 0.55 + 0.35 * f
                    sprite(cr, HEART, x_c + CAT_W * 0.6 + 16 * (f - 0.5) * (1 if i % 2 else -1), sit_y + 10 - 12 * f, k)
        else:
            f = clamp((t - M_LEAP) / 0.6)
            x, y = jump(f, (x_c, sit_y), (lx + size + 270, out_y), 44)
            nyan(cr, x, y, t=t, run=True, air=f <= 0.9, sx=1 - 0.12 * math.sin(math.pi * f), sy=1 + 0.25 * math.sin(math.pi * f), rot=0.45 * math.cos(math.pi * f))

    return draw, M_END


SCENES = (courier, check, mine)
