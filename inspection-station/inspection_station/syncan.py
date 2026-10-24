"""
Synthetic top view of an open, empty can - for the Synthetic camera source and for testing.

Seen from above, an empty can shows (outside -> in):
    flange / curl   bright ring at the outer edge        (outer circle ... flange inner circle)
    neck            narrow dark band
    side wall       the inside of the can wall, in perspective
    bottom rim      thin bright ring                     (bottom circle)
    bottom dome     the can bottom
Around it: the conveyor, guide rails, bolts and the next can at the edge of the
view - things the inspection must ignore (the inspection area).

render_can(kind) -> (8-bit gray image, truth dict). kind: None (good can), "Rim dent",
"Flange damage", "Out of round", "Side wall dent", "Contamination", "Scratch".
"""
from __future__ import annotations

import math
import random

import cv2
import numpy as np

KINDS = ["Rim dent", "Flange damage", "Out of round", "Side wall dent", "Contamination", "Scratch"]

# ring boundaries as fractions of the outer radius
FLANGE_IN, NECK_IN, BOTTOM_OUT, BOTTOM_IN = 0.93, 0.88, 0.70, 0.66


def _grid(W, H):
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    return xx, yy


def render_can(kind: str | None = None, W: int = 1024, H: int = 768, seed: int | None = None,
               clutter: bool = True, grid=None, severity: float = 1.0):
    """severity < 1 makes defects smaller / fainter (for testing the detection limits)."""
    rnd = random.Random(seed)
    sv = severity
    xx, yy = grid if grid is not None else _grid(W, H)
    cx, cy = W / 2 + rnd.uniform(-35, 35), H / 2 + rnd.uniform(-22, 22)
    R = H * 0.36 * rnd.uniform(0.985, 1.015)
    dx, dy = xx - cx, yy - cy
    theta = np.arctan2(dy, dx)
    truth = {"kind": kind or "", "zone": "", "x": None, "y": None, "cx": cx, "cy": cy, "R": R}

    # shape of the can: radius as a function of angle (dents / out-of-round change it)
    scale = np.ones_like(xx)
    if kind == "Out of round":
        e, phi = rnd.uniform(0.035, 0.06) * sv, rnd.uniform(0, math.pi)
        rot_x = dx * math.cos(phi) + dy * math.sin(phi)
        rot_y = -dx * math.sin(phi) + dy * math.cos(phi)
        rho = np.sqrt((rot_x / (1 + e)) ** 2 + (rot_y / (1 - e)) ** 2) / R
        truth.update(zone="rim", x=cx, y=cy)
    else:
        rho = np.sqrt(dx ** 2 + dy ** 2) / R
    if kind == "Rim dent":
        a0, depth, width = rnd.uniform(-math.pi, math.pi), rnd.uniform(0.035, 0.06) * sv, rnd.uniform(0.10, 0.16)
        dang = np.angle(np.exp(1j * (theta - a0)))
        dent = 1 - depth * np.exp(-(dang / width) ** 2) * np.clip((rho - 0.80) / 0.15, 0, 1)
        rho = rho / dent
        truth.update(zone="rim", x=cx + R * math.cos(a0), y=cy + R * math.sin(a0))

    # lighting: a little brighter on one side, like a real light
    light = 1 + 0.07 * np.cos(theta - 0.6) + 0.03 * (dx / R)

    # conveyor + clutter
    img = 58 + 8 * np.sin(xx / 7.0) * 0.4 + 6 * (yy / H)
    if clutter:
        for y0 in (0.06, 0.94):                                       # guide rails
            img = np.where(np.abs(yy - H * y0) < H * 0.025, 190 + 25 * np.cos((yy - H * y0) / 6), img)
        for bx, by in ((0.08, 0.2), (0.93, 0.78), (0.9, 0.2)):        # bolts
            img = np.where((xx - W * bx) ** 2 + (yy - H * by) ** 2 < 14 ** 2, 210, img)
        nx = -R * 0.55 if rnd.random() < 0.5 else W + R * 0.55        # next can, half out of view
        dn = np.sqrt((xx - nx) ** 2 + (yy - cy) ** 2) / R
        img = np.where(dn < 1, np.where(dn > FLANGE_IN, 225, 130), img)

    can = np.full_like(xx, 0)
    can = np.where(rho < 1.0, 225 - 60 * ((rho - 0.965) / 0.035) ** 2, can)         # flange highlight
    can = np.where(rho < FLANGE_IN, 70, can)                                        # neck shadow
    can = np.where(rho < NECK_IN, 95 + 35 * (NECK_IN - rho) / (NECK_IN - BOTTOM_OUT), can)   # side wall
    can = np.where(rho < BOTTOM_OUT, 200, can)                                      # bottom rim
    can = np.where(rho < BOTTOM_IN, 140 + 30 * (rho / BOTTOM_IN) ** 2, can)         # bottom dome
    can = np.where(np.abs(rho - 0.15) < 0.01, can - 18, can)                        # dome centre ring
    can = can * light
    img = np.where(rho < 1.0, can, img)

    layer = np.zeros_like(xx)
    if kind == "Flange damage":
        a0, w = rnd.uniform(-math.pi, math.pi), rnd.uniform(0.06, 0.11) * sv
        dang = np.abs(np.angle(np.exp(1j * (theta - a0))))
        nick = (dang < w) & (rho > FLANGE_IN - 0.005) & (rho < 1.0)
        img = np.where(nick, 62, img)                                               # missing / bent curl
        truth.update(zone="flange", x=cx + R * 0.965 * math.cos(a0), y=cy + R * 0.965 * math.sin(a0))
    elif kind == "Side wall dent":
        a0, rr = rnd.uniform(-math.pi, math.pi), rnd.uniform(0.74, 0.84) * R
        px, py = cx + rr * math.cos(a0), cy + rr * math.sin(a0)
        s = rnd.uniform(9, 14)
        layer += rnd.choice([-45, 40]) * sv * np.exp(-((xx - px) ** 2 + (yy - py) ** 2) / (2 * s ** 2))
        truth.update(zone="side wall", x=px, y=py)
    elif kind == "Contamination":
        a0, rr = rnd.uniform(-math.pi, math.pi), rnd.uniform(0.1, 0.5) * R
        px, py = cx + rr * math.cos(a0), cy + rr * math.sin(a0)
        for _ in range(rnd.randint(2, 4)):
            c = (int(px + rnd.uniform(-22, 22)), int(py + rnd.uniform(-22, 22)))
            cv2.circle(layer, c, max(3, int(rnd.randint(5, 9) * sv)), -85 * sv, -1, cv2.LINE_AA)
        truth.update(zone="bottom", x=px, y=py)
    elif kind == "Scratch":
        a0, rr = rnd.uniform(-math.pi, math.pi), rnd.uniform(0.1, 0.4) * R
        px, py = cx + rr * math.cos(a0), cy + rr * math.sin(a0)
        L, a2 = rnd.uniform(60, 110), rnd.uniform(0, math.pi)
        cv2.line(layer, (int(px), int(py)), (int(px + L * math.cos(a2)), int(py + L * math.sin(a2))),
                 -75 * sv, 3, cv2.LINE_AA)
        truth.update(zone="bottom", x=px + L / 2 * math.cos(a2), y=py + L / 2 * math.sin(a2))
    img = img + np.where(rho < 0.99, layer, 0)
    img = cv2.GaussianBlur(np.clip(img, 0, 255).astype(np.float32), (3, 3), 0.8)   # optics
    return np.clip(img, 0, 255).astype(np.uint8), truth
