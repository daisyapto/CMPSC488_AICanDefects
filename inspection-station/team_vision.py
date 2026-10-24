"""
team_vision.py - the team's OpenCV can inspection (top view of an open, empty can).

Builds on edgeDetection.py / edgeFaultDetection.py (Hough circle for the rim,
circularity, diameter against a reference) and adds:

  * Only the CAN is inspected. The station crops the image to the inspection
    area you draw on the camera view, and inside it the can is chosen by how
    complete and strong its rim edge is - rails, bolts and the next can at the
    edge of the view are ignored.
  * Precise circles instead of one Hough circle: 720 rays from the centre find
    the OUTER flange edge, the flange INNER edge and the BOTTOM rim with
    sub-pixel accuracy, then circles / an ellipse are fitted robustly.
  * Rim checks: dents (local radius deviation), out-of-round (ellipse),
    missing / bent flange (edge gap), off-centre inner circles (crushed or
    tilted can), optional size check against a reference diameter.
  * Surface checks per ZONE - flange, side wall, bottom. The can is "unrolled"
    around its centre (polar image); a good can looks the same all the way
    round, so each spot is compared with the same radius at the angles around
    it. Gradual lighting changes are allowed; local spots stand out.

Choose it in the dashboard: Settings > Detection > Detection engine > Team.
Tune the numbers in SETTINGS below; the station reloads this file when it is saved.

THE CONTRACT (what the station needs)
-------------------------------------
    detect(image) -> result
    image   BGR numpy array (inside the inspection area, full camera resolution)
    result  [] for a good can, or a list of defects
              [{"label": "Rim dent", "box": (x, y, w, h), "confidence": 0.9}, ...]
            or a dict with extra drawing / numbers for the dashboard:
              {"defects": [...], "circles": [{"x", "y", "r", "label"}, ...],
               "measurements": {"Diameter": "512 px", ...}}
    Raise an exception = the can is rejected (fail-safe).

TEST ON A LAPTOP
    python team_vision.py folder_or_images...     writes *_result.png with circles and boxes
    python team_vision.py --demo                  runs on generated test cans and scores itself
"""
import math
import time

import cv2
import numpy as np

# =============================================================================
# SETTINGS - tune for your camera, lens and lighting
# =============================================================================
WORK_WIDTH = 640               # processing resolution (px); bigger = finer but slower. Thin scratches:
                               # draw the inspection area tight around the can (keeps more camera pixels)
CAN_SIZE = (0.2, 0.98)         # can diameter as a fraction of the area's shorter side (search range)
FLANGE_INNER = (0.86, 0.97)    # where the flange's inner edge is (fraction of the outer radius)
BOTTOM_EDGE = (0.55, 0.84)     # where the bottom rim is
ROUNDNESS_TOL = 1.5            # % of radius: a local bump/dip in the rim bigger than this = dent
OVAL_TOL = 2.5                 # %: (longest - shortest diameter) / diameter = out of round
CONCENTRIC_TOL = 4.0           # % of radius: inner circles this far off-centre = crushed / tilted can
EDGE_GAP = 0.35                # rim edge weaker than this fraction of normal = missing / bent flange
SURFACE_SENSITIVITY = 16       # %: how much darker/brighter a spot must be than the same ring around it
MIN_DEFECT_AREA = 0.00025      # fraction of the can's area; smaller spots are ignored
REFERENCE_DIAMETER_PX = None   # e.g. 1012 (full camera px): check the can size against it
DIAMETER_TOL = 3.0             # % size tolerance for REFERENCE_DIAMETER_PX
PIXELS_PER_MM = None           # e.g. 15.3: report sizes in mm too
NO_CAN = "reject"              # no can found in the inspection area: "reject" or "pass"

N_ANG = 720                    # rays around the can (0.5 degree steps; 360 is faster but misses small defects)


# =============================================================================
# helpers
# =============================================================================
def _polar(img, cx, cy, r0, r1, n_rad, n_ang=None):
    """Sample img along rays: rows = angles, cols = radius r0..r1. Also returns validity."""
    n_ang = n_ang or N_ANG
    ang = np.linspace(0, 2 * np.pi, n_ang, endpoint=False, dtype=np.float32)
    rad = np.linspace(r0, r1, n_rad, dtype=np.float32)
    mx = (cx + np.cos(ang)[:, None] * rad[None, :]).astype(np.float32)
    my = (cy + np.sin(ang)[:, None] * rad[None, :]).astype(np.float32)
    h, w = img.shape[:2]
    valid = (mx >= 0) & (mx <= w - 1) & (my >= 0) & (my <= h - 1)
    p = cv2.remap(img, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return p, valid, ang, rad


def _fit_circle(x, y):
    """Algebraic (Kasa) least-squares circle: returns cx, cy, r."""
    A = np.column_stack([x, y, np.ones_like(x)])
    b = x ** 2 + y ** 2
    sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    cx, cy = sol[0] / 2, sol[1] / 2
    return cx, cy, math.sqrt(max(sol[2] + cx ** 2 + cy ** 2, 1e-6))


def _robust_circle(x, y, iters=3):
    keep = np.ones(len(x), bool)
    cx = cy = r = 0.0
    for _ in range(iters):
        if keep.sum() < 10:
            break
        cx, cy, r = _fit_circle(x[keep], y[keep])
        res = np.hypot(x - cx, y - cy) - r
        mad = np.median(np.abs(res[keep] - np.median(res[keep]))) + 1e-6
        keep = np.abs(res) < max(3.0 * 1.4826 * mad, 0.75)
    return cx, cy, r, keep


def _runs(flags):
    """Contiguous True runs on a circular array -> list of (start, length)."""
    n = len(flags)
    if flags.all():
        return [(0, n)]
    if not flags.any():
        return []
    start = int(np.argmin(flags))             # begin at a False so runs don't wrap
    f = np.roll(flags, -start)
    out, i = [], 0
    while i < n:
        if f[i]:
            j = i
            while j < n and f[j]:
                j += 1
            out.append(((i + start) % n, j - i))
            i = j
        else:
            i += 1
    return out


def _edge_along_rays(grad_polar, rad, expected, spread):
    """Strongest radial edge per ray, weighted toward the expected radius; sub-pixel."""
    w = np.exp(-((rad - expected) / spread) ** 2)[None, :]
    score = grad_polar * w
    k = np.argmax(score, axis=1)
    k = np.clip(k, 1, len(rad) - 2)
    rows = np.arange(len(k))
    y0, y1, y2 = score[rows, k - 1], score[rows, k], score[rows, k + 1]
    den = y0 - 2 * y1 + y2
    with np.errstate(divide="ignore", invalid="ignore"):
        off = np.where(np.abs(den) > 1e-6, 0.5 * (y0 - y2) / den, 0)
    step = rad[1] - rad[0]
    return rad[k] + np.clip(off, -1, 1) * step, grad_polar[rows, k]


def _find_can(gb, mag):
    """Coarse can circle: Hough candidates scored by a complete, strong rim; prefer the outermost
    of concentric circles (the bottom rim is a circle too)."""
    f = min(1.0, 360 / gb.shape[1])                     # rough circle only: work small (fast)
    if f < 1:
        gb = cv2.resize(gb, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
        mag = cv2.resize(mag, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
    h, w = gb.shape
    m = min(h, w)
    cands = cv2.HoughCircles(gb, cv2.HOUGH_GRADIENT, dp=1.5, minDist=m * 0.05, param1=100, param2=28,
                             minRadius=int(m * CAN_SIZE[0] / 2), maxRadius=int(m * CAN_SIZE[1] / 2))
    if cands is None:
        return None
    ang = np.linspace(0, 2 * np.pi, 180, endpoint=False)
    scored = []
    for x, y, r in cands[0][:25]:
        px, py = x + r * np.cos(ang), y + r * np.sin(ang)
        inside = (px >= 0) & (px < w - 1) & (py >= 0) & (py < h - 1)
        if inside.mean() < 0.97:                       # cut off by the area edge: not the can
            continue
        g = mag[py[inside].astype(int), px[inside].astype(int)]
        complete = (g > 0.3 * np.median(g)).mean()
        scored.append((float(np.median(g) * complete), float(x), float(y), float(r)))
    if not scored:
        return None
    scored.sort(reverse=True)
    return [(x / f, y / f, r / f) for _, x, y, r in scored[:6]]


def _outer_edge(gb32, cx, cy, r0):

    # ---- 1. outer flange edge ------------------------------------------------------------
    # Hough gives one circle per centre, often the bottom rim. Unroll the gradient around that
    # centre and take the OUTERMOST ring whose edge goes all the way round (rails and bolts don't).
    h, w = gb32.shape
    reach = min(1.7 * r0, max(cx, w - cx, cy, h - cy))
    ipol0, v0, ang, rad0 = _polar(gb32, cx, cy, 0.4 * r0, reach, int(reach - 0.4 * r0))
    d0 = np.diff(cv2.GaussianBlur(ipol0, (3, 1), 0), axis=1)
    full = v0[:, 1:].mean(axis=0) > 0.97                       # radii fully inside the area
    prof = np.where(full, np.abs(np.median(np.where(v0[:, 1:], d0, np.nan), axis=0)), 0)
    prof = np.nan_to_num(prof)
    peaks = [k for k in range(2, len(prof) - 2) if prof[k] == prof[k - 2:k + 3].max() and prof[k] > 0]
    if peaks:
        top = max(prof[k] for k in peaks)
        ring_r = [rad0[k] for k in peaks if prof[k] > 0.25 * top]
        r0 = max(ring_r)                                       # outermost complete ring = can outline
    k0 = int(np.argmin(np.abs(rad0[:-1] - r0)))
    polarity = np.sign(np.median(d0[:, max(0, k0 - 2):k0 + 3]))  # bright flange -> background (usually -)
    if polarity == 0:
        polarity = -1.0

    ra0, ra1 = 0.88 * r0, 1.10 * r0
    ipol1, valid, ang, rad = _polar(gb32, cx, cy, ra0, ra1, int(ra1 - ra0) * 2)
    deriv = np.gradient(cv2.GaussianBlur(ipol1, (3, 1), 0), axis=1) * polarity
    r_out, strength = _edge_along_rays(np.clip(deriv, 0, None), rad, r0, 0.05 * r0)
    inside = valid.all(axis=1)
    norm = np.median(strength[inside]) + 1e-6
    good = inside & (strength > EDGE_GAP * norm)
    return r_out, strength, inside, good, ang


def _refine(gb32, cx, cy, r0):
    """From a rough circle to the measured outer rim (re-centring up to 4 times).
    quality ~1 = a complete, clean circular edge; low = probably not the can's rim."""
    for _ in range(4):
        r_out, strength, inside, good, ang = _outer_edge(gb32, cx, cy, r0)
        if good.sum() < N_ANG * 0.4:
            return None
        ex, ey = cx + r_out * np.cos(ang), cy + r_out * np.sin(ang)
        ncx, ncy, nr, _ = _robust_circle(ex[good], ey[good])
        if math.hypot(ncx - cx, ncy - cy) < 0.01 * nr:
            break
        cx, cy, r0 = ncx, ncy, nr
    else:
        r_out, strength, inside, good, ang = _outer_edge(gb32, cx, cy, r0)
    ex, ey = cx + r_out * np.cos(ang), cy + r_out * np.sin(ang)
    if good.sum() < N_ANG * 0.4:
        return None
    fx, fy, fr, keep = _robust_circle(ex[good], ey[good])
    # share of the rim lying on a smooth outline - an ellipse, so an oval can still counts as found
    pts = np.column_stack([ex[good][keep], ey[good][keep]]).astype(np.float32) if keep.sum() >= 5 else \
        np.column_stack([ex[good], ey[good]]).astype(np.float32)
    (ecx, ecy), (ea, eb), eang = cv2.fitEllipse(pts)
    tt = np.arctan2(ey[good] - ecy, ex[good] - ecx) - math.radians(eang)
    r_e = 1 / np.sqrt((np.cos(tt) / (ea / 2)) ** 2 + (np.sin(tt) / (eb / 2)) ** 2)
    res = np.abs(np.hypot(ex[good] - ecx, ey[good] - ecy) - r_e)
    clean = float(np.mean(res < 0.006 * fr))
    q = good.mean() * clean
    return {"cx": cx, "cy": cy, "r_out": r_out, "strength": strength, "inside": inside, "good": good,
            "ang": ang, "quality": q}


# =============================================================================
# detect
# =============================================================================
def detect(image):
    t0 = time.perf_counter()
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    H, W = gray.shape
    s = WORK_WIDTH / W if W > WORK_WIDTH else 1.0
    g = cv2.resize(gray, (round(W * s), round(H * s)), interpolation=cv2.INTER_AREA) if s != 1 else gray
    gb = cv2.GaussianBlur(g, (5, 5), 0)
    gx = cv2.Sobel(gb, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gb, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)

    def box(px, py, pad=6):                            # work-image points -> full-res (x, y, w, h)
        x0, x1 = max(0, np.min(px) - pad), min(g.shape[1] - 1, np.max(px) + pad)
        y0, y1 = max(0, np.min(py) - pad), min(g.shape[0] - 1, np.max(py) + pad)
        return (int(x0 / s), int(y0 / s), int((x1 - x0) / s) + 1, int((y1 - y0) / s) + 1)

    cands = _find_can(gb, mag)
    if not cands:
        if NO_CAN == "pass":
            return []
        return {"defects": [{"label": "No can in area", "box": (0, 0, W, H), "confidence": 0.9}],
                "measurements": {"Can": "not found"}}
    gb32 = gb.astype(np.float32)
    best, tried = None, []
    for cand in cands:                                 # usually the first one; others only if it's poor
        if any(math.hypot(cand[0] - t[0], cand[1] - t[1]) < 0.06 * t[2] and abs(cand[2] - t[2]) < 0.1 * t[2]
               for t in tried):
            continue                                   # would end on the same circle again
        fit = _refine(gb32, *cand)
        tried.append(cand)
        if fit is None:
            continue
        if best is None or fit["quality"] > best["quality"]:
            best = fit
        if fit["quality"] > 0.7:
            break
    if best is None:
        raise RuntimeError("can rim not clear enough to measure - check focus / lighting / inspection area")
    cx, cy, r_out, strength, inside, good, ang = (best[k] for k in ("cx", "cy", "r_out", "strength", "inside", "good", "ang"))
    ex, ey = cx + r_out * np.cos(ang), cy + r_out * np.sin(ang)
    if good.sum() < N_ANG * 0.4:
        raise RuntimeError("can rim not clear enough to measure - check focus / lighting / inspection area")
    ocx, ocy, R, inl = _robust_circle(ex[good], ey[good])

    # ellipse for out-of-round; rim dents measured against the ellipse (so ovality isn't counted twice)
    pts = np.column_stack([ex[good][inl], ey[good][inl]]).astype(np.float32)
    (ecx, ecy), (ea, eb), eang = cv2.fitEllipse(pts)
    oval = abs(ea - eb) / ((ea + eb) / 2) * 100
    phi = math.radians(eang)
    t = np.arctan2(ey - ecy, ex - ecx)
    tt = t - phi
    r_ell = 1 / np.sqrt((np.cos(tt) / (ea / 2)) ** 2 + (np.sin(tt) / (eb / 2)) ** 2)
    res = np.hypot(ex - ecx, ey - ecy) - r_ell
    res_s = cv2.medianBlur(np.tile(res.astype(np.float32), 3)[None, :], 5)[0, N_ANG:2 * N_ANG]   # smooth over 5 rays
    defects = []
    tol_px = ROUNDNESS_TOL / 100 * R
    for start, n in _runs(good & (np.abs(res_s) > tol_px)):
        if n < 3:
            continue
        idx = (start + np.arange(n)) % N_ANG
        depth = float(np.max(np.abs(res_s[idx])))
        defects.append({"label": "Rim dent", "box": box(ex[idx], ey[idx], pad=10),
                        "confidence": float(min(0.99, 0.6 + depth / tol_px / 10)), "_z": 0})
    for start, n in _runs(inside & ~good):
        if n < 4:
            continue
        idx = (start + np.arange(n)) % N_ANG
        rr = R * 0.965
        defects.append({"label": "Flange damage",
                        "box": box(ocx + rr * np.cos(ang[idx]), ocy + rr * np.sin(ang[idx]), pad=14),
                        "confidence": 0.9, "_z": 0})
    if oval > OVAL_TOL:
        defects.append({"label": "Out of round", "box": box(ex[good], ey[good], pad=4),
                        "confidence": float(min(0.99, 0.6 + oval / OVAL_TOL / 10)), "_z": 0})

    # ---- 2. inner circles from the unrolled can -----------------------------------------
    n_rad = int(R)
    ipol, ivalid, ang, irad = _polar(gb.astype(np.float32), ocx, ocy, 0, R * 1.0, n_rad)
    prof = np.median(ipol[ivalid.all(axis=1)] if ivalid.all(axis=1).any() else ipol, axis=0)
    dprof = np.abs(np.gradient(cv2.GaussianBlur(prof[None, :], (5, 1), 0)[0]))

    def ring(lo, hi):
        a, b = int(lo * n_rad), int(hi * n_rad)
        k = a + int(np.argmax(dprof[a:b]))
        dpol = np.abs(np.diff(cv2.GaussianBlur(ipol, (3, 1), 0), axis=1))
        rr, st = _edge_along_rays(np.pad(dpol, ((0, 0), (0, 1))), irad, irad[k], 0.02 * R)
        ok = st > 0.3 * np.median(st)
        x, y = ocx + rr * np.cos(ang), ocy + rr * np.sin(ang)
        fx, fy, fr, _ = _robust_circle(x[ok], y[ok]) if ok.sum() > 50 else (ocx, ocy, irad[k], None)
        return rr, fx, fy, fr

    r_fl, fx, fy, fr = ring(*FLANGE_INNER)
    r_bt, bx, by, br = ring(*BOTTOM_EDGE)
    off = max(math.hypot(fx - ocx, fy - ocy), math.hypot(bx - ocx, by - ocy)) / R * 100
    if off > CONCENTRIC_TOL:
        defects.append({"label": "Crushed / off-centre", "box": box(ex[good], ey[good], pad=4),
                        "confidence": float(min(0.99, 0.6 + off / CONCENTRIC_TOL / 10)), "_z": 0})

    # ---- 3. surface: flange, side wall, bottom ------------------------------------------
    # expected = rolling median of the same radius over the nearby angles (a good can is the
    # same all the way round; slow lighting changes pass, local spots stand out)
    # medians over a 45-degree window every 8 degrees, then interpolated round the can (fast)
    small = cv2.resize(ipol, (max(8, n_rad // 2), 360), interpolation=cv2.INTER_AREA)
    win, step = 45, 8
    padded = np.concatenate([small[-win:], small, small[:win]], axis=0)
    centres = np.arange(0, 360, step)
    med = np.stack([np.median(padded[c + win - win // 2:c + win + win // 2 + 1], axis=0) for c in centres])
    med = np.concatenate([med, med[:1]], axis=0)                       # close the circle
    a = np.arange(N_ANG) * 360.0 / N_ANG / step
    i0 = np.floor(a).astype(int)
    fr = (a - i0)[:, None]
    expected = cv2.resize(((1 - fr) * med[i0] + fr * med[i0 + 1]).astype(np.float32), (n_rad, N_ANG),
                          interpolation=cv2.INTER_LINEAR)
    diff = ipol - expected
    rr = irad[None, :]
    band = max(2.0, 0.015 * R)
    r_o = np.hypot(ex - ocx, ey - ocy)                 # outer edge seen from the fitted centre
    near_edge = ((np.abs(rr - r_fl[:, None]) < band) | (np.abs(rr - r_bt[:, None]) < band) |
                 (rr > r_o[:, None] - band * 1.5))
    zone = np.where(rr > r_fl[:, None], 0, np.where(rr > r_bt[:, None], 1, 2))     # 0 flange, 1 wall, 2 bottom
    # relative to the local brightness, so exposure / gain changes don't change the sensitivity;
    # smoothed first, so sensor noise doesn't make speckles
    rel = cv2.GaussianBlur(diff, (5, 5), 0) / np.maximum(expected, 20.0)
    noise = 1.4826 * np.median(np.abs(rel[~near_edge])) + 1e-6
    thr = max(SURFACE_SENSITIVITY / 100, 4.5 * noise)
    diff = rel
    mask = ((np.abs(rel) > thr) & ~near_edge & ivalid).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, lab, stats, cent = cv2.connectedComponentsWithStats(mask, 8)
    can_area = math.pi * R * R
    dtheta = 2 * math.pi / N_ANG
    for i in range(1, n):
        ys, xs = np.nonzero(lab == i)
        area = float(np.sum(irad[xs] * dtheta * (irad[1] - irad[0])))
        if area < MIN_DEFECT_AREA * can_area:
            continue
        px, py = ocx + irad[xs] * np.cos(ang[ys]), ocy + irad[xs] * np.sin(ang[ys])
        z = int(np.bincount(zone[ys, xs], minlength=3).argmax())
        strength = float(np.mean(np.abs(diff[ys, xs])))
        if z == 0:
            label = "Flange damage"
        elif z == 1:
            label = "Side wall dent"
        else:
            pts = np.column_stack([px, py]).astype(np.float32)
            (_, _), (rw, rh), _ = cv2.minAreaRect(pts)
            darker = float(np.mean(diff[ys, xs])) < 0
            if max(rw, rh) / max(1.0, min(rw, rh)) >= 3.5:
                label = "Scratch"
            elif darker and area < 0.01 * can_area:
                label = "Contamination"
            else:
                label = "Bottom dent"
        defects.append({"label": label, "box": box(px, py),
                        "confidence": float(min(0.99, 0.55 + strength / thr / 6)), "_z": 1})

    # ---- 4. size against a reference -----------------------------------------------------
    diameter = 2 * R / s
    if REFERENCE_DIAMETER_PX:
        dev = (diameter - REFERENCE_DIAMETER_PX) / REFERENCE_DIAMETER_PX * 100
        if abs(dev) > DIAMETER_TOL:
            defects.append({"label": "Wrong size", "box": box(ex[good], ey[good], pad=4),
                            "confidence": 0.95, "_z": 0})

    # merge overlapping findings (a rim dent also disturbs the flange surface next to it)
    defects.sort(key=lambda d: d["_z"])
    merged = []
    for d in defects:
        x, y, w, h = d["box"]
        if any(x < mx + mw + 8 and mx < x + w + 8 and y < my + mh + 8 and my < y + h + 8
               for mx, my, mw, mh in (m["box"] for m in merged)) and d["_z"] == 1:
            continue
        merged.append(d)
    for d in merged:
        d.pop("_z", None)

    roundness = float(np.max(np.abs(res_s[good]))) / R * 100
    meas = {"Diameter": f"{diameter:.0f} px" + (f" / {diameter / PIXELS_PER_MM:.2f} mm" if PIXELS_PER_MM else ""),
            "Roundness": f"{roundness:.2f} % (limit {ROUNDNESS_TOL})",
            "Oval": f"{oval:.2f} % (limit {OVAL_TOL})",
            "Centre offset": f"{off:.2f} % (limit {CONCENTRIC_TOL})",
            "Rim edge seen": f"{good.mean() * 100:.0f} %",
            "Time": f"{(time.perf_counter() - t0) * 1000:.0f} ms"}
    circles = [{"x": float(x) / s, "y": float(y) / s, "r": float(np.ravel(r)[0]) / s, "label": lab}
               for x, y, r, lab in ((ocx, ocy, R, "outer"), (fx, fy, fr, "flange inner"), (bx, by, br, "bottom"))]
    return {"defects": merged, "circles": circles, "measurements": meas}


# =============================================================================
# Laptop test runner - not used by the station
# =============================================================================
def _draw(img, out):
    drawn = img.copy() if img.ndim == 3 else cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    res = out if isinstance(out, dict) else {"defects": out}
    for c in res.get("circles", []):
        cv2.circle(drawn, (int(c["x"]), int(c["y"])), int(c["r"]), (238, 211, 34), 1, cv2.LINE_AA)
    for d in res.get("defects", []):
        x, y, w, h = map(int, d["box"])
        cv2.rectangle(drawn, (x, y), (x + w, y + h), (0, 0, 255), 2)
        cv2.putText(drawn, d["label"], (x, max(15, y - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
    for i, (k, v) in enumerate(res.get("measurements", {}).items()):
        cv2.putText(drawn, f"{k}: {v}", (10, 22 + 20 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    return drawn


def _demo(n=70):
    """Score the detection on generated cans (inspection_station/syncan.py)."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from inspection_station.syncan import KINDS, render_can
    import random
    kinds = [None] * (n // 2) + [KINDS[i % len(KINDS)] for i in range(n - n // 2)]
    random.Random(1).shuffle(kinds)
    tp = fn = fp = tn = 0
    times, misses, false = [], [], []
    for i, k in enumerate(kinds):
        img, truth = render_can(k, seed=1000 + i)
        t = time.perf_counter()
        out = detect(cv2.cvtColor(img, cv2.COLOR_GRAY2BGR))
        times.append((time.perf_counter() - t) * 1000)
        found = out["defects"] if isinstance(out, dict) else out
        if k and found:
            tp += 1
        elif k:
            fn += 1
            misses.append(k)
        elif found:
            fp += 1
            false.append(", ".join(d["label"] for d in found))
        else:
            tn += 1
    print(f"defective cans caught: {tp}/{tp + fn}   good cans passed: {tn}/{tn + fp}   "
          f"time: median {np.median(times):.0f} ms, max {max(times):.0f} ms")
    if misses:
        print("missed:", misses)
    if false:
        print("false rejects:", false)


if __name__ == "__main__":
    import sys
    from pathlib import Path

    if "--demo" in sys.argv:
        _demo()
        sys.exit()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    files = []
    for a in args:
        p = Path(a)
        files += sorted(f for f in p.iterdir() if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff")) \
            if p.is_dir() else [p]
    if not files:
        sys.exit("usage: python team_vision.py IMAGE_OR_FOLDER [...] [--show]   or   python team_vision.py --demo")
    for f in files:
        if f.stem.endswith("_result"):
            continue
        img = cv2.imread(str(f))
        if img is None:
            print(f"{f.name}: can't read")
            continue
        t = time.perf_counter()
        try:
            out = detect(img.copy())
        except Exception as e:
            print(f"{f.name}: ERROR {e}")
            continue
        ms = (time.perf_counter() - t) * 1000
        found = out["defects"] if isinstance(out, dict) else out
        verdict = "PASS" if not found else "FAIL " + ", ".join(d["label"] for d in found)
        dst = f.with_name(f.stem + "_result.png")
        cv2.imwrite(str(dst), _draw(img, out))
        print(f"{f.name}: {verdict}  ({ms:.0f} ms)  -> {dst.name}")
        if "--show" in sys.argv:
            cv2.imshow("team_vision", _draw(img, out))
            if cv2.waitKey(0) & 0xFF == 27:
                break
    if "--show" in sys.argv:
        cv2.destroyAllWindows()
