"""
Operator dashboard (HMI) for the can inspection station - Tkinter, no extra packages.

Layout
  top bar   : station name, line state, GPIO / sensor / camera status, clock
  left      : line controls, live stack light + reject valve, maintenance
  center    : last can, KPIs, timeline of recent cans, tabs (table / defects / log)
  right     : settings (inspector, camera IP, reject timing) - auto-saved
"""
from __future__ import annotations

import csv
import time
from collections import Counter, deque
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path
from tkinter import font as tkfont

try:                                   # camera view needs OpenCV; Pillow is optional
    import cv2
except Exception:
    cv2 = None
try:
    from PIL import Image, ImageTk
except Exception:
    Image = ImageTk = None

from .config import DATA_DIR, REJECT_PIN, SENSOR_PIN
from .controller import (SOURCES, CSV_HEADER, DISABLED, ESCAPED, FIRED, LOCKED, MISSED, PASS, PENDING,
                         InspectionResult, result_row)

# =============================================================================
# Theme
# =============================================================================
BG = "#0b0f15"
SURFACE = "#131a23"
SURFACE2 = "#1a2330"
RAISED = "#232e3d"
BORDER = "#253041"
TEXT = "#e6edf3"
MUTED = "#8b98a9"
DIM = "#4f5b6d"

BLUE = "#3b82f6"
GREEN = "#22c55e"
AMBER = "#f59e0b"
RED = "#ef4444"
ORANGE = "#fb923c"
CYAN = "#22d3ee"
VIOLET = "#a78bfa"

LAMP_COLORS = {"red": RED, "yellow": AMBER, "green": GREEN}
REJECT_LABEL = {PASS: "", PENDING: "pending", FIRED: "fired", MISSED: "MISSED",
                DISABLED: "off", LOCKED: "LOCKED"}
MAX_TABLE_ROWS = 300
MAX_LOG_LINES = 600
LOG_COLORS = {"INFO": MUTED, "WARN": AMBER, "ERROR": RED, "ALARM": ORANGE}


def _rgb(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def blend(c1: str, c2: str, t: float) -> str:
    """Mix c1 toward c2 by t (0..1)."""
    a, b = _rgb(c1), _rgb(c2)
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def lighten(c, t=0.15):
    return blend(c, "#ffffff", t)


def round_rect(cv: tk.Canvas, x0, y0, x1, y1, r, **kw):
    r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
           x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return cv.create_polygon(pts, smooth=True, **kw)


def to_photo(bgr):
    """OpenCV BGR image -> Tk image (Pillow if installed, else PPM)."""
    if ImageTk is not None:
        return ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)))
    ok, buf = cv2.imencode(".ppm", bgr)
    return tk.PhotoImage(data=buf.tobytes(), format="ppm")


def fit(bgr, w, h):
    """Resize to fit inside w x h, keeping aspect ratio."""
    ih, iw = bgr.shape[:2]
    s = min(w / iw, h / ih)
    nw, nh = max(1, int(iw * s)), max(1, int(ih * s))
    return cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)


class F:
    """Fonts - resolved at startup from what is installed (Pi: DejaVu)."""


def init_fonts(root):
    fams = {f.lower(): f for f in tkfont.families(root)}

    def pick(names, fallback):
        return next((fams[n.lower()] for n in names if n.lower() in fams), fallback)

    sans = pick(("Inter", "Segoe UI", "SF Pro Text", "Helvetica Neue", "Roboto", "Noto Sans",
                 "DejaVu Sans", "Liberation Sans", "Helvetica"), "TkDefaultFont")
    mono = pick(("JetBrains Mono", "SF Mono", "Cascadia Mono", "Consolas", "DejaVu Sans Mono",
                 "Liberation Mono", "Courier New", "Courier"), "TkFixedFont")
    F.sans, F.mono_family = sans, mono
    F.title = (sans, 15, "bold")
    F.sub = (sans, 9)
    F.h = (sans, 8, "bold")
    F.body = (sans, 10)
    F.body_b = (sans, 10, "bold")
    F.small = (sans, 8)
    F.chip = (sans, 8, "bold")
    F.btn = (sans, 10, "bold")
    F.btn_big = (sans, 12, "bold")
    F.estop = (sans, 17, "bold")
    F.state = (sans, 12, "bold")
    F.hero = (sans, 34, "bold")
    F.kpi = (mono, 18, "bold")
    F.mono = (mono, 9)
    F.clock = (mono, 13, "bold")


# =============================================================================
# Small widgets
# =============================================================================
class FlatButton(tk.Label):
    """Flat, colored, touch-friendly button that looks the same on every OS."""

    def __init__(self, parent, text, command=None, color=RAISED, fg=TEXT, font=None,
                 padx=12, pady=8, **kw):
        super().__init__(parent, text=text, bg=color, fg=fg, font=font or F.btn,
                         padx=padx, pady=pady, cursor="hand2", **kw)
        self.command, self.color, self.fg = command, color, fg
        self.enabled, self._hover = True, False
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)

    def _paint(self):
        if not self.enabled:
            self.config(bg=SURFACE2, fg=DIM, cursor="arrow")
        else:
            self.config(bg=lighten(self.color, 0.12) if self._hover else self.color,
                        fg=self.fg, cursor="hand2")

    def _set_hover(self, on):
        self._hover = on
        self._paint()

    def _press(self, _):
        if self.enabled:
            self.config(bg=blend(self.color, "#000000", 0.2))

    def _release(self, e):
        self._paint()
        inside = 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height()
        if self.enabled and inside and self.command:
            self.command()

    def set_enabled(self, on: bool):
        if on != self.enabled:
            self.enabled = on
            self._paint()


class Toggle(tk.Canvas):
    """iOS-style switch."""
    W, H = 40, 22

    def __init__(self, parent, value=False, command=None, color=GREEN, bg=SURFACE):
        super().__init__(parent, width=self.W, height=self.H, bg=bg,
                         highlightthickness=0, cursor="hand2")
        self.value, self.command, self.color = bool(value), command, color
        self.bind("<Button-1>", self._click)
        self._draw()

    def _draw(self):
        self.delete("all")
        h, r = self.H, self.H / 2
        track = self.color if self.value else BORDER
        round_rect(self, 0, 0, self.W, h, r, fill=track, outline=track)
        x = self.W - r if self.value else r
        self.create_oval(x - r + 3, 3, x + r - 3, h - 3, fill="#ffffff", outline="")

    def _click(self, _):
        self.set(not self.value)
        if self.command:
            self.command(self.value)

    def set(self, value):
        value = bool(value)
        if value != self.value:
            self.value = value
            self._draw()


class Stepper(tk.Frame):
    """[-] value [+] numeric input, touch friendly."""

    def __init__(self, parent, value, lo, hi, step=1, unit="", on_change=None, bg=SURFACE,
                 entry_width=5, unit_width=3):
        super().__init__(parent, bg=bg)
        self.lo, self.hi, self.step, self.on_change = lo, hi, step, on_change
        self.current = value
        self.var = tk.StringVar(value=str(value))
        FlatButton(self, "−", lambda: self._set(self.current - step), padx=8, pady=2).pack(side="left")
        e = tk.Entry(self, textvariable=self.var, width=entry_width, justify="center", font=F.mono,
                     bg=SURFACE2, fg=TEXT, insertbackground=TEXT, relief="flat",
                     highlightthickness=1, highlightbackground=BORDER, highlightcolor=BLUE)
        e.pack(side="left", padx=4, ipady=3)
        FlatButton(self, "+", lambda: self._set(self.current + step), padx=8, pady=2).pack(side="left")
        tk.Label(self, text=unit, font=F.small, fg=MUTED, bg=bg, width=unit_width,
                 anchor="w").pack(side="left", padx=(5, 0))
        e.bind("<Return>", self._commit)
        e.bind("<FocusOut>", self._commit)

    def _commit(self, _=None):
        try:
            self._set(int(float(self.var.get())))
        except ValueError:
            self.var.set(str(self.current))

    def _set(self, v):
        v = max(self.lo, min(self.hi, v))
        self.var.set(str(v))
        if v != self.current:
            self.current = v
            if self.on_change:
                self.on_change(v)


class Segmented(tk.Frame):
    """Segmented control: one of several options."""

    def __init__(self, parent, options, value, command=None):
        super().__init__(parent, bg=SURFACE2, highlightthickness=1, highlightbackground=BORDER)
        self.command, self.value, self.btns = command, value, {}
        for key, label in options:
            b = tk.Label(self, text=label, font=F.chip, padx=10, pady=5, cursor="hand2")
            b.pack(side="left")
            b.bind("<Button-1>", lambda e, k=key: self._click(k))
            self.btns[key] = b
        self._paint()

    def _click(self, key):
        if key == self.value:
            return
        prev, self.value = self.value, key
        self._paint()
        if self.command and self.command(key) is False:     # command can veto
            self.set(prev)

    def set(self, key):
        self.value = key
        self._paint()

    def _paint(self):
        for key, b in self.btns.items():
            sel = key == self.value
            b.config(bg=BLUE if sel else SURFACE2, fg="#ffffff" if sel else MUTED)


class ScrollFrame(tk.Frame):
    """Vertically scrollable container; put children in .inner."""

    def __init__(self, parent, bg=SURFACE):
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", self._update)
        self.canvas.bind("<Configure>", self._update)
        self.bind_all("<MouseWheel>", self._wheel, add="+")
        self.bind_all("<Button-4>", lambda e: self._scroll(e, -1), add="+")
        self.bind_all("<Button-5>", lambda e: self._scroll(e, 1), add="+")

    def _update(self, _=None):
        self.canvas.itemconfigure(self._win, width=self.canvas.winfo_width())
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        needs = self.inner.winfo_reqheight() > self.canvas.winfo_height() + 2
        if needs and not self.bar.winfo_ismapped():
            self.bar.pack(side="right", fill="y", padx=(6, 0), before=self.canvas)
        elif not needs and self.bar.winfo_ismapped():
            self.bar.pack_forget()
            self.canvas.yview_moveto(0)

    def _inside(self, e):
        w = self.winfo_containing(e.x_root, e.y_root)
        while w is not None:
            if w is self:
                return True
            w = w.master
        return False

    def _scroll(self, e, direction):
        if self.bar.winfo_ismapped() and self._inside(e):
            self.canvas.yview_scroll(direction * 2, "units")

    def _wheel(self, e):
        self._scroll(e, -1 if e.delta > 0 else 1)


class Chip(tk.Frame):
    """Status chip in the top bar: ● LABEL value"""

    def __init__(self, parent, label):
        super().__init__(parent, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER)
        self.dot = tk.Label(self, text="●", font=F.small, bg=SURFACE, fg=DIM)
        self.dot.pack(side="left", padx=(9, 3), pady=5)
        tk.Label(self, text=label, font=F.chip, fg=MUTED, bg=SURFACE).pack(side="left")
        self.val = tk.Label(self, text="", font=F.small, fg=TEXT, bg=SURFACE)
        self.val.pack(side="left", padx=(5, 10))
        self._last = None

    def set(self, text, color):
        if (text, color) != self._last:
            self._last = (text, color)
            self.val.config(text=text)
            self.dot.config(fg=color)


class KpiTile(tk.Frame):
    def __init__(self, parent, label, accent):
        super().__init__(parent, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER)
        tk.Frame(self, bg=accent, width=3).pack(side="left", fill="y")
        body = tk.Frame(self, bg=SURFACE)
        body.pack(side="left", fill="both", expand=True, padx=12, pady=9)
        tk.Label(body, text=label.upper(), font=F.h, fg=MUTED, bg=SURFACE, anchor="w").pack(fill="x")
        self.value = tk.Label(body, text="0", font=F.kpi, fg=TEXT, bg=SURFACE, anchor="w")
        self.value.pack(fill="x")
        self.sub = tk.Label(body, text=" ", font=F.small, fg=DIM, bg=SURFACE, anchor="w")
        self.sub.pack(fill="x")

    def set(self, value, sub=" ", color=TEXT, sub_color=DIM):
        self.value.config(text=str(value), fg=color)
        self.sub.config(text=sub or " ", fg=sub_color)


def panel(parent, title=None, pad=14):
    """Bordered surface with an optional small-caps title. Returns frame; use .body."""
    outer = tk.Frame(parent, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER)
    outer.hdr = tk.Frame(outer, bg=SURFACE)
    if title:
        outer.hdr.pack(fill="x", padx=pad, pady=(11, 7))
        tk.Label(outer.hdr, text=title.upper(), font=F.h, fg=MUTED, bg=SURFACE).pack(side="left")
    outer.body = tk.Frame(outer, bg=SURFACE)
    outer.body.pack(fill="both", expand=True, padx=pad, pady=(0 if title else pad, pad))
    return outer


# =============================================================================
# Canvas visuals
# =============================================================================
class StackLightTower(tk.Canvas):
    """Live drawing of the stack light with the meaning of each lamp.
    Lamps are clickable in manual override."""
    ORDER = ("red", "yellow", "green")
    MEANING = {"red": ("FAULT", "solid until fixed"),
               "yellow": ("DEFECT", "flashing = warning"),
               "green": ("RUNNING", "flashing = ready")}
    W, LAMP_H, GAP, TOP, X0, X1 = 234, 40, 5, 20, 14, 74

    def __init__(self, parent, on_click=None):
        h = self.TOP + 3 * (self.LAMP_H + self.GAP) + 40
        super().__init__(parent, width=self.W, height=h, bg=SURFACE, highlightthickness=0)
        self.on_click = on_click
        self.state = {k: False for k in self.ORDER}
        self.clickable = False
        self.bind("<Button-1>", self._click)
        self._draw()

    def set_state(self, state: dict, clickable: bool):
        if state != self.state or clickable != self.clickable:
            self.state, self.clickable = dict(state), clickable
            self.config(cursor="hand2" if clickable else "")
            self._draw()

    def _lamp_box(self, i):
        y0 = self.TOP + i * (self.LAMP_H + self.GAP)
        return self.X0, y0, self.X1, y0 + self.LAMP_H

    def _draw(self):
        self.delete("all")
        cx = (self.X0 + self.X1) / 2
        round_rect(self, self.X0 + 3, 6, self.X1 - 3, self.TOP + 4, 6, fill=RAISED, outline="")
        for i, key in enumerate(self.ORDER):
            x0, y0, x1, y1 = self._lamp_box(i)
            color, on = LAMP_COLORS[key], self.state.get(key)
            if on:  # soft glow
                round_rect(self, x0 - 10, y0 - 5, x1 + 10, y1 + 5, 14, fill=blend(color, SURFACE, 0.86), outline="")
                round_rect(self, x0 - 5, y0 - 2, x1 + 5, y1 + 2, 10, fill=blend(color, SURFACE, 0.7), outline="")
            fill = color if on else blend(color, SURFACE, 0.8)
            round_rect(self, x0, y0, x1, y1, 7, fill=fill, outline=blend(color, SURFACE, 0.45 if on else 0.65))
            round_rect(self, x0 + 7, y0 + 7, x0 + 13, y1 - 7, 3,
                       fill=lighten(fill, 0.45 if on else 0.06), outline="")
            if self.clickable:
                self.create_text(x1 - 8, (y0 + y1) / 2, text="ON" if on else "OFF",
                                 font=F.chip, fill="#0b0f15" if on else MUTED, anchor="e")
            title, sub = self.MEANING[key]
            ym = (y0 + y1) / 2
            self.create_text(x1 + 18, ym - 7, text=title, anchor="w", font=F.chip,
                             fill=color if on else MUTED)
            self.create_text(x1 + 18, ym + 8, text=sub, anchor="w", font=F.small,
                             fill=TEXT if on else DIM)
        base_y = self.TOP + 3 * (self.LAMP_H + self.GAP)
        self.create_rectangle(cx - 5, base_y, cx + 5, base_y + 26, fill=RAISED, outline="")
        round_rect(self, cx - 30, base_y + 26, cx + 30, base_y + 37, 4, fill=RAISED, outline="")

    def _click(self, e):
        if not self.clickable or not self.on_click:
            return
        for i, key in enumerate(self.ORDER):
            x0, y0, x1, y1 = self._lamp_box(i)
            if x0 <= e.x <= x1 and y0 <= e.y <= y1:
                self.on_click(key)


class Timeline(tk.Canvas):
    """One cell per can, newest on the right."""
    CELL, GAP, H = 12, 3, 30

    def __init__(self, parent):
        super().__init__(parent, height=self.H, bg=SURFACE, highlightthickness=0)
        self.items: deque = deque(maxlen=400)
        self.bind("<Configure>", lambda e: self.redraw())

    def push(self, r):
        self.items.append(r)
        self.redraw()

    def clear(self):
        self.items.clear()
        self.redraw()

    @staticmethod
    def _color(r):
        if r.passed:
            return GREEN, GREEN
        if r.reject == PENDING:
            return SURFACE, RED
        if r.reject in ESCAPED:
            return ORANGE, ORANGE
        return RED, RED

    def redraw(self):
        self.delete("all")
        w = self.winfo_width()
        step = self.CELL + self.GAP
        n = max(1, (w + self.GAP) // step)
        shown = list(self.items)[-n:]
        offset = n - len(shown)
        y0, y1 = 4, self.H - 4
        for i in range(n):
            x0 = i * step
            if i < offset:
                round_rect(self, x0, y0, x0 + self.CELL, y1, 3, fill=SURFACE2, outline="")
            else:
                fill, outline = self._color(shown[i - offset])
                round_rect(self, x0, y0, x0 + self.CELL, y1, 3, fill=fill, outline=outline)


class DefectBars(tk.Canvas):
    def __init__(self, parent):
        super().__init__(parent, bg=SURFACE, highlightthickness=0)
        self.counts: Counter = Counter()
        self.total = 0
        self.bind("<Configure>", lambda e: self.redraw())

    def set(self, counts: Counter, total_inspected: int):
        self.counts, self.total = counts, total_inspected
        self.redraw()

    def redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if not self.counts:
            self.create_text(w / 2, h / 2, text="No defects recorded this session",
                             fill=DIM, font=F.body)
            return
        items = self.counts.most_common()
        peak = items[0][1]
        label_w, right_w, row_h = 150, 110, 34
        bar_w = max(40, w - label_w - right_w - 20)
        for i, (name, count) in enumerate(items):
            y = 18 + i * row_h
            if y + row_h > h:
                break
            self.create_text(0, y + 10, text=name, fill=TEXT, font=F.body, anchor="w")
            round_rect(self, label_w, y, label_w + bar_w, y + 20, 5, fill=SURFACE2, outline="")
            length = max(10, bar_w * count / peak)
            color = blend(RED, ORANGE, i / max(1, len(items) - 1) * 0.6)
            round_rect(self, label_w, y, label_w + length, y + 20, 5, fill=color, outline="")
            pct = count / self.total * 100 if self.total else 0
            self.create_text(label_w + bar_w + 14, y + 10, anchor="w", fill=TEXT, font=F.mono,
                             text=f"{count:>4}   {pct:4.1f}%")


# =============================================================================
# Dashboard
# =============================================================================
class Dashboard:
    def __init__(self, root: tk.Tk, ctl, events, fullscreen=False):
        self.root, self.ctl, self.events = root, ctl, events
        init_fonts(root)
        self._init_style()
        root.title("Can Inspection Station")
        root.configure(bg=BG)
        root.geometry("1400x860")
        root.minsize(1260, 780)
        if fullscreen:
            root.attributes("-fullscreen", True)

        # session statistics (the full record is written to data/ by the controller)
        self.results: dict[int, InspectionResult] = {}
        self.recent = deque()
        self.n = self.npass = self.nfail = self.nfired = self.nescaped = 0
        self.sum_ms = 0.0
        self.defects: Counter = Counter()
        self.session_start = datetime.now()
        self.last_id = None
        self._save_job = None

        self._bind_keys()
        self._build_topbar()
        main = tk.Frame(root, bg=BG)
        main.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        main.columnconfigure(0, minsize=264)
        main.columnconfigure(1, weight=1)
        main.columnconfigure(2, minsize=340)
        main.rowconfigure(0, weight=1)
        left = tk.Frame(main, bg=BG, width=264)
        center = tk.Frame(main, bg=BG)
        right = tk.Frame(main, bg=BG, width=340)
        for side in (left, right):          # fixed widths: nothing shifts when states change
            side.pack_propagate(False)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        center.grid(row=0, column=1, sticky="nsew")
        right.grid(row=0, column=2, sticky="nsew", padx=(14, 0))
        self._build_left(left)
        self._build_center(center)
        self._build_right(right)
        self._tick()

    # ------------------------------------------------------------------ style
    def _init_style(self):
        st = ttk.Style()
        st.theme_use("clam")
        st.configure("Treeview", background=SURFACE, fieldbackground=SURFACE, foreground=TEXT,
                     rowheight=26, borderwidth=0, font=F.body)
        st.configure("Treeview.Heading", background=SURFACE2, foreground=MUTED, relief="flat",
                     font=F.h, borderwidth=0, padding=(6, 6))
        st.map("Treeview.Heading", background=[("active", RAISED)])
        st.map("Treeview", background=[("selected", blend(BLUE, SURFACE, 0.55))],
               foreground=[("selected", TEXT)])
        st.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        st.configure("Vertical.TScrollbar", background=RAISED, troughcolor=SURFACE,
                     bordercolor=SURFACE, lightcolor=RAISED, darkcolor=RAISED,
                     arrowcolor=MUTED, gripcount=0)
        st.configure("TNotebook", background=BG, borderwidth=1, tabmargins=(0, 0, 0, 0),
                     bordercolor=BORDER, lightcolor=SURFACE, darkcolor=SURFACE)
        st.configure("TNotebook.Tab", background=BG, foreground=MUTED, padding=(18, 8),
                     borderwidth=1, font=F.body_b, lightcolor=BG, darkcolor=BG, bordercolor=BG)
        st.map("TNotebook.Tab",
               background=[("selected", SURFACE), ("active", SURFACE2)],
               foreground=[("selected", TEXT), ("active", TEXT)],
               lightcolor=[("selected", SURFACE)], darkcolor=[("selected", SURFACE)],
               bordercolor=[("selected", BORDER)], expand=[("selected", (0, 0, 0, 0))])

    def _bind_keys(self):
        def guarded(fn):
            def handler(e):
                if isinstance(e.widget, tk.Entry):
                    return
                fn()
            return handler
        r = self.root
        r.bind("<Escape>", lambda e: self.ctl.emergency_stop())     # always works
        r.bind("<F5>", guarded(self.ctl.start))
        r.bind("<F6>", guarded(self.ctl.stop))
        r.bind("<space>", guarded(lambda: self.ctl.running and self.ctl.simulate_can()))
        r.bind("<F11>", lambda e: r.attributes("-fullscreen", not r.attributes("-fullscreen")))
        r.bind("<Control-s>", lambda e: self.ctl.save_settings(announce=True))

    # --------------------------------------------------------------- top bar
    def _build_topbar(self):
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill="x", padx=16, pady=(14, 14))

        logo = tk.Canvas(bar, width=38, height=38, bg=BG, highlightthickness=0)
        logo.pack(side="left")
        round_rect(logo, 1, 1, 37, 37, 10, fill=BLUE, outline="")
        round_rect(logo, 12, 8, 26, 30, 4, fill="#ffffff", outline="")
        logo.create_line(12, 12, 26, 12, fill=BLUE, width=2)
        logo.create_line(12, 26, 26, 26, fill=BLUE, width=2)

        title = tk.Frame(bar, bg=BG)
        title.pack(side="left", padx=(12, 0))
        tk.Label(title, text="Can Inspection Station", font=F.title, fg=TEXT, bg=BG).pack(anchor="w")
        tk.Label(title, text="Yards Brewing  ·  empty can defect line", font=F.sub,
                 fg=MUTED, bg=BG).pack(anchor="w")

        right = tk.Frame(bar, bg=BG)
        right.pack(side="right")
        clock = tk.Frame(right, bg=BG)
        clock.pack(side="right", padx=(16, 0))
        self.clock = tk.Label(clock, font=F.clock, fg=TEXT, bg=BG)
        self.clock.pack(anchor="e")
        self.uptime = tk.Label(clock, font=F.small, fg=MUTED, bg=BG)
        self.uptime.pack(anchor="e")
        self.chip_cam = Chip(right, "CAMERA")
        self.chip_cam.pack(side="right", padx=(8, 0))
        self.chip_sensor = Chip(right, "SENSOR")
        self.chip_sensor.pack(side="right", padx=(8, 0))
        self.chip_io = Chip(right, "GPIO")
        self.chip_io.pack(side="right")

        self.state_pill = tk.Label(bar, font=F.state, padx=22, pady=7, anchor="w")
        self.state_pill.pack(side="left", padx=(28, 16))     # packed last: chips keep their room

    # ------------------------------------------------------------------ left
    def _build_left(self, col):
        p = panel(col, "Line control")
        p.pack(fill="x")
        b = p.body
        self.b_start = FlatButton(b, "▶   START", self.ctl.start, color=GREEN, fg="#052e14",
                                  font=F.btn_big, pady=11)
        self.b_start.pack(fill="x")
        self.b_stop = FlatButton(b, "■   STOP", self.ctl.stop, color=AMBER, fg="#3a2300",
                                 font=F.btn_big, pady=11)
        self.b_stop.pack(fill="x", pady=(6, 0))
        self.b_estop = FlatButton(b, "E-STOP", self.ctl.emergency_stop, color=RED,
                                  font=F.estop, pady=15)
        self.b_estop.pack(fill="x", pady=(10, 0))
        row = tk.Frame(b, bg=SURFACE)
        row.pack(fill="x", pady=(6, 0))
        row.columnconfigure((0, 1), weight=1, uniform="r")
        self.b_reset = FlatButton(row, "Reset", self.ctl.reset, color=RAISED)
        self.b_reset.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        self.b_ack = FlatButton(row, "Acknowledge", self.ctl.acknowledge, color=AMBER, fg="#3a2300")
        self.b_ack.grid(row=0, column=1, sticky="ew", padx=(3, 0))
        self.b_sim = FlatButton(b, "+  Simulate can", self.ctl.simulate_can, color=RAISED, fg=CYAN)
        self.b_sim.pack(fill="x", pady=(6, 0))

        p = panel(col, "Stack light & reject valve")
        p.pack(fill="both", expand=True, pady=(14, 0))
        b = p.body
        self.tower = StackLightTower(b, on_click=self.ctl.manual_toggle)
        self.tower.pack(anchor="w")
        self.status_box = tk.Frame(b, bg=SURFACE)
        self.status_box.pack(fill="x", pady=(2, 10))
        self._status_items = None

        valve = tk.Frame(b, bg=SURFACE2)
        valve.pack(fill="x")
        self.valve_dot = tk.Label(valve, text="●", font=F.body, fg=DIM, bg=SURFACE2)
        self.valve_dot.pack(side="left", padx=(10, 6), pady=7)
        vt = tk.Frame(valve, bg=SURFACE2)
        vt.pack(side="left", pady=5)
        tk.Label(vt, text="REJECT VALVE", font=F.chip, fg=TEXT, bg=SURFACE2).pack(anchor="w")
        tk.Label(vt, text=f"GPIO {REJECT_PIN} · relay 4", font=F.small, fg=MUTED,
                 bg=SURFACE2).pack(anchor="w")
        self.valve_txt = tk.Label(valve, text="CLOSED", font=F.chip, fg=MUTED, bg=SURFACE2,
                                  width=11, anchor="e")
        self.valve_txt.pack(side="right", padx=10)

        ov = tk.Frame(b, bg=SURFACE)
        ov.pack(fill="x", pady=(12, 0))
        ot = tk.Frame(ov, bg=SURFACE)
        ot.pack(side="left")
        tk.Label(ot, text="Manual override", font=F.body_b, fg=TEXT, bg=SURFACE).pack(anchor="w")
        tk.Label(ot, text="click lamps to switch them", font=F.small, fg=MUTED, bg=SURFACE).pack(anchor="w")
        self.override = Toggle(ov, False, self._override_changed, color=BLUE)
        self.override.pack(side="right")
        self.b_burst = FlatButton(b, "Test reject burst", self.ctl.test_burst, color=RAISED, fg=RED)
        self.b_burst.pack(fill="x", pady=(10, 0))

    # ---------------------------------------------------------------- center
    def _build_center(self, col):
        col.columnconfigure(0, weight=1)
        col.rowconfigure(3, weight=1)

        top = tk.Frame(col, bg=BG)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)

        hero = tk.Frame(top, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER)
        hero.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        self.hero_accent = tk.Frame(hero, bg=DIM, width=6)
        self.hero_accent.pack(side="left", fill="y")
        hb = tk.Frame(hero, bg=SURFACE)
        hb.pack(side="left", fill="both", expand=True, padx=20, pady=14)
        tk.Label(hb, text="LAST CAN", font=F.h, fg=MUTED, bg=SURFACE).pack(anchor="w")
        self.hero_big = tk.Label(hb, text="—", font=F.hero, fg=DIM, bg=SURFACE)
        self.hero_big.pack(anchor="w")
        self.hero_detail = tk.Label(hb, text="Waiting for the first can", font=F.body, fg=TEXT, bg=SURFACE)
        self.hero_detail.pack(anchor="w")
        self.hero_meta = tk.Label(hb, text=" ", font=F.small, fg=MUTED, bg=SURFACE)
        self.hero_meta.pack(anchor="w", pady=(2, 0))

        sensor = tk.Frame(top, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER)
        sensor.grid(row=0, column=1, sticky="nsew")
        sb = tk.Frame(sensor, bg=SURFACE)
        sb.pack(padx=26, pady=12)
        tk.Label(sb, text="TRIGGER SENSOR", font=F.h, fg=MUTED, bg=SURFACE).pack()
        self.sensor_cv = tk.Canvas(sb, width=76, height=76, bg=SURFACE, highlightthickness=0)
        self.sensor_cv.pack(pady=4)
        self.sensor_lbl = tk.Label(sb, text="CLEAR", font=F.body_b, fg=MUTED, bg=SURFACE)
        self.sensor_lbl.pack()
        tk.Label(sb, text=f"GPIO {SENSOR_PIN}", font=F.small, fg=DIM, bg=SURFACE).pack()
        self._draw_sensor(False)

        kpis = tk.Frame(col, bg=BG)
        kpis.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        tiles = [("Inspected", BLUE), ("Passed", GREEN), ("Rejected", RED),
                 ("Yield", CYAN), ("Cans / min", VIOLET), ("Inspect", AMBER)]
        self.kpi = {}
        for i, (label, accent) in enumerate(tiles):
            kpis.columnconfigure(i, weight=1, uniform="k")
            t = KpiTile(kpis, label, accent)
            t.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 10, 0))
            self.kpi[label] = t

        tl = panel(col, "Recent cans")
        tl.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        legend = tk.Frame(tl.hdr, bg=SURFACE)
        legend.pack(side="right")
        for text, color in (("pass", GREEN), ("rejected", RED), ("not rejected", ORANGE)):
            tk.Label(legend, text="■", font=F.body, fg=color, bg=SURFACE).pack(side="left", padx=(10, 3))
            tk.Label(legend, text=text, font=F.small, fg=MUTED, bg=SURFACE).pack(side="left")
        self.timeline = Timeline(tl.body)
        self.timeline.pack(fill="x")
        self._center_top = (top, kpis, tl)      # hidden while the Camera tab is open
        self._compact = False

        nb = ttk.Notebook(col)
        nb.grid(row=3, column=0, sticky="nsew", pady=(14, 0))

        # tab: inspections
        t1 = tk.Frame(nb, bg=SURFACE)
        nb.add(t1, text="Inspections")
        bar = tk.Frame(t1, bg=SURFACE)
        bar.pack(fill="x", padx=12, pady=(10, 8))
        self.table_info = tk.Label(bar, text="0 cans this session", font=F.small, fg=MUTED, bg=SURFACE)
        self.table_info.pack(side="left")
        FlatButton(bar, "Clear session", self._clear, padx=10, pady=4, font=F.small).pack(side="right")
        FlatButton(bar, "Export CSV", self._export, color=BLUE, padx=10, pady=4,
                   font=F.small).pack(side="right", padx=(0, 6))
        tf = tk.Frame(t1, bg=SURFACE)
        tf.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        cols = ("id", "time", "result", "defect", "conf", "ms", "reject", "img")
        heads = ("CAN", "TIME", "RESULT", "DEFECT", "CONF.", "INSPECT", "REJECT", "IMG")
        widths = (64, 84, 84, 160, 64, 76, 84, 44)
        self.tree = ttk.Treeview(tf, columns=cols, show="headings")
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h, anchor="w" if c == "defect" else "center")
            self.tree.column(c, width=w, minwidth=50, anchor="w" if c == "defect" else "center")
        self.tree.tag_configure("pass", foreground=blend(TEXT, MUTED, 0.3))
        self.tree.tag_configure("fail", foreground=RED, background=blend(RED, SURFACE, 0.9))
        self.tree.tag_configure("escaped", foreground=ORANGE, background=blend(ORANGE, SURFACE, 0.85))
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<Double-1>", self._open_row_image)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # tab: camera
        self._build_camera_tab(nb)

        # tab: defects
        t2 = tk.Frame(nb, bg=SURFACE)
        nb.add(t2, text="Defects")
        self.bars = DefectBars(t2)
        self.bars.pack(fill="both", expand=True, padx=18, pady=14)

        # tab: event log
        t3 = tk.Frame(nb, bg=SURFACE)
        nb.add(t3, text="Event log")
        lf = tk.Frame(t3, bg=SURFACE)
        lf.pack(fill="both", expand=True, padx=12, pady=12)
        self.log_txt = tk.Text(lf, bg=SURFACE, fg=TEXT, font=F.mono, relief="flat", wrap="word",
                               state="disabled", highlightthickness=0, padx=4, pady=2,
                               spacing1=2, spacing3=2)
        lsb = ttk.Scrollbar(lf, orient="vertical", command=self.log_txt.yview)
        self.log_txt.configure(yscrollcommand=lsb.set)
        self.log_txt.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")
        for level, color in LOG_COLORS.items():
            self.log_txt.tag_configure(level, foreground=color)
        self.log_txt.tag_configure("ts", foreground=DIM)
        self.nb, self.log_tab = nb, t3
        nb.bind("<<NotebookTabChanged>>", self._tab_changed)

    def _tab_changed(self, _):
        sel = self.nb.select()
        if sel == str(self.log_tab):
            self.nb.tab(self.log_tab, text="Event log")
        cam = sel == str(self.cam_tab)
        if cam != self._compact:                       # give the camera the whole center
            self._compact = cam
            for w in self._center_top:
                w.grid_remove() if cam else w.grid()
        m = self.ctl.cammgr
        if m is not None:
            m.live_visible = cam
            if cam and self._cam_job is None:
                self._camera_tick()

    # ----------------------------------------------------------------- right
    def _build_right(self, col):
        p = panel(col, "Settings")
        p.pack(fill="both", expand=True)
        sf = ScrollFrame(p.body)                 # scrolls on short screens
        sf.pack(fill="both", expand=True)
        b = sf.inner
        s = self.ctl.settings

        self._section(b, "Inspector", first=True)
        self.insp_rows = {}
        for name, insp in self.ctl.inspectors.items():
            self._inspector_row(b, name, insp.description)
        self._paint_inspectors()
        self._field(b, "Simulated defect rate",
                    Stepper(b, s.sim_defect_rate, 0, 100, 5, "%", self._set_rate))

        self._section(b, "Camera  ·  Ethernet")
        ip = tk.Frame(b, bg=SURFACE)
        ip.pack(fill="x")
        self.cam_ip = tk.StringVar(value=s.camera_ip)
        e = self.cam_ip_entry = tk.Entry(ip, textvariable=self.cam_ip, font=F.mono, bg=SURFACE2, fg=TEXT,
                     insertbackground=TEXT, relief="flat", highlightthickness=1,
                     highlightbackground=BORDER, highlightcolor=BLUE)
        e.pack(side="left", fill="x", expand=True, ipady=5)
        e.bind("<Return>", lambda ev: self._apply_ip())
        FlatButton(ip, "Apply", self._apply_ip, color=BLUE, padx=12, pady=5).pack(side="left", padx=(6, 0))
        cs = tk.Frame(b, bg=SURFACE)
        cs.pack(fill="x", pady=(8, 0))
        self.cam_dot = tk.Label(cs, text="●", font=F.body, fg=DIM, bg=SURFACE)
        self.cam_dot.pack(side="left")
        self.cam_txt = tk.Label(cs, text="Checking…", font=F.body, fg=MUTED, bg=SURFACE)
        self.cam_txt.pack(side="left", padx=(4, 0))
        FlatButton(cs, "Check", self.ctl.check_camera, padx=10, pady=3, font=F.small).pack(side="right")

        self._section(b, "Reject")
        self._toggle_row(b, "Reject enabled", "air fires on FAIL", "reject_enabled", GREEN)
        self._toggle_row(b, "Single-shot latch", "lock after one fire (air test)", "single_shot", AMBER)
        self._field(b, "Reject delay", Stepper(b, s.reject_delay_ms, 0, 5000, 5, "ms",
                                               lambda v: self._set("reject_delay_ms", v)))
        self._field(b, "Air pulses", Stepper(b, s.pulses, 1, 10, 1, "",
                                             lambda v: self._set("pulses", v)))
        self._field(b, "Pulse on", Stepper(b, s.pulse_on_ms, 5, 500, 5, "ms",
                                           lambda v: self._set("pulse_on_ms", v)))
        self._field(b, "Pulse off", Stepper(b, s.pulse_off_ms, 5, 500, 5, "ms",
                                            lambda v: self._set("pulse_off_ms", v)))
        self._section(b, "Defect warning (yellow flashing)")
        self._field(b, "Defects in a row", Stepper(b, s.alarm_after, 1, 50, 1, "",
                                                   lambda v: self._set("alarm_after", v)))
        self._field(b, f"Rate over last {s.warn_window}", Stepper(b, s.warn_rate_pct, 5, 100, 5, "%",
                                                       lambda v: self._set("warn_rate_pct", v)))
        self.burst_hint = tk.Label(b, font=F.small, fg=MUTED, bg=SURFACE, justify="left", anchor="w")
        self.burst_hint.pack(fill="x", pady=(6, 0))
        self._update_burst_hint()

        self.saved_lbl = tk.Label(b, text="Changes are saved automatically", font=F.small,
                                  fg=DIM, bg=SURFACE, anchor="w")
        self.saved_lbl.pack(fill="x", pady=(10, 0))

    def _section(self, parent, text, first=False):
        if not first:
            tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", pady=(14, 12))
        tk.Label(parent, text=text.upper(), font=F.h, fg=BLUE, bg=SURFACE).pack(anchor="w", pady=(0, 6))

    def _field(self, parent, label, widget):
        row = tk.Frame(parent, bg=SURFACE)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label, font=F.body, fg=TEXT, bg=SURFACE).pack(side="left")
        widget.pack(in_=row, side="right")
        widget.lift()

    def _toggle_row(self, parent, label, sub, key, color):
        row = tk.Frame(parent, bg=SURFACE)
        row.pack(fill="x", pady=4)
        t = tk.Frame(row, bg=SURFACE)
        t.pack(side="left")
        tk.Label(t, text=label, font=F.body, fg=TEXT, bg=SURFACE).pack(anchor="w")
        tk.Label(t, text=sub, font=F.small, fg=MUTED, bg=SURFACE).pack(anchor="w")
        Toggle(row, getattr(self.ctl.settings, key), lambda v, k=key: self._set(k, v),
               color=color).pack(side="right")

    def _inspector_row(self, parent, name, desc):
        row = tk.Frame(parent, bg=SURFACE, cursor="hand2")
        row.pack(fill="x", pady=1)
        dot = tk.Label(row, font=F.body, bg=SURFACE)
        dot.pack(side="left", padx=(8, 8), pady=5)
        txt = tk.Frame(row, bg=SURFACE)
        txt.pack(side="left", fill="x", pady=4)
        l1 = tk.Label(txt, text=name, font=F.body_b, fg=TEXT, bg=SURFACE, anchor="w")
        l1.pack(fill="x")
        l2 = tk.Label(txt, text=desc, font=F.small, fg=MUTED, bg=SURFACE, anchor="w")
        l2.pack(fill="x")
        widgets = (row, dot, txt, l1, l2)
        for w in widgets:
            w.bind("<Button-1>", lambda e, n=name: self._pick_inspector(n))
        self.insp_rows[name] = (widgets, dot)

    def _paint_inspectors(self):
        for name, (widgets, dot) in self.insp_rows.items():
            sel = name == self.ctl.settings.inspector
            bg = SURFACE2 if sel else SURFACE
            for w in widgets:
                w.config(bg=bg)
            dot.config(text="◉" if sel else "○", fg=BLUE if sel else DIM)

    # ------------------------------------------------------------ camera tab
    def _build_camera_tab(self, nb):
        t = tk.Frame(nb, bg=SURFACE)
        nb.add(t, text="Camera")
        self.cam_tab = t
        self._cam_job = None
        self._live_photo = self._last_photo = None
        self._shown_live = self._shown_last = -1
        m = self.ctl.cammgr
        if m is None or cv2 is None:
            tk.Label(t, text="Camera features need OpenCV and NumPy", font=F.body_b,
                     fg=TEXT, bg=SURFACE).pack(pady=(70, 10))
            tk.Label(t, text="Laptop:        pip install opencv-python numpy pillow\n"
                             "Raspberry Pi:  sudo apt install python3-opencv python3-numpy "
                             "python3-pil.imagetk\n\n" + (self.ctl.camera_import_error or ""),
                     font=F.mono, fg=MUTED, bg=SURFACE, justify="left").pack()
            return
        s = self.ctl.settings

        top = tk.Frame(t, bg=SURFACE)
        top.pack(fill="x", padx=12, pady=(10, 8))
        tk.Label(top, text="SOURCE", font=F.h, fg=MUTED, bg=SURFACE).pack(side="left", padx=(0, 8))
        self.seg_source = Segmented(top, list(SOURCES.items()), s.camera_backend, self._pick_source)
        self.seg_source.pack(side="left")
        self.cam_state_txt = tk.Label(top, font=F.small, fg=MUTED, bg=SURFACE, anchor="e")
        self.cam_state_txt.pack(side="right")
        self.cam_state_dot = tk.Label(top, text="●", font=F.body, fg=DIM, bg=SURFACE)
        self.cam_state_dot.pack(side="right", padx=(0, 4))

        body = tk.Frame(t, bg=SURFACE)
        body.pack(fill="both", expand=True, padx=12)
        side = tk.Frame(body, bg=SURFACE, width=238)
        side.pack(side="right", fill="y", padx=(12, 0))
        side.pack_propagate(False)
        self.view_cv = tk.Canvas(body, bg="#05070a", highlightthickness=1, highlightbackground=BORDER)
        self.view_cv.pack(side="left", fill="both", expand=True)

        tk.Label(side, text="LAST INSPECTED CAN", font=F.h, fg=MUTED, bg=SURFACE).pack(anchor="w")
        self.last_cv = tk.Canvas(side, width=234, height=176, bg="#05070a", highlightthickness=1,
                                 highlightbackground=BORDER, cursor="hand2")
        self.last_cv.pack(anchor="w", pady=(6, 6))
        self.last_cv.bind("<Button-1>", lambda e: self._open_last_image())
        self.last_big = tk.Label(side, text="—", font=F.btn_big, fg=DIM, bg=SURFACE)
        self.last_big.pack(anchor="w")
        self.last_info = tk.Label(side, text="No can inspected with the camera yet.\n"
                                             "Select the Camera inspector and start the line.",
                                  font=F.small, fg=MUTED, bg=SURFACE, justify="left",
                                  anchor="w", wraplength=232)
        self.last_info.pack(fill="x")
        tk.Frame(side, bg=BORDER, height=1).pack(fill="x", pady=10)
        tk.Label(side, text="GOOD-CAN REFERENCE", font=F.h, fg=MUTED, bg=SURFACE).pack(anchor="w")
        self.ref_lbl = tk.Label(side, font=F.small, fg=MUTED, bg=SURFACE, justify="left",
                                anchor="w", wraplength=232)
        self.ref_lbl.pack(fill="x", pady=(4, 8))
        rb = tk.Frame(side, bg=SURFACE)
        rb.pack(fill="x")
        FlatButton(rb, "Teach good can", self._teach, color=BLUE, font=F.small, padx=10, pady=5).pack(side="left")
        FlatButton(rb, "Clear", self._clear_ref, font=F.small, padx=10, pady=5).pack(side="left", padx=(6, 0))
        self._paint_ref()

        bar = tk.Frame(t, bg=SURFACE)
        bar.pack(fill="x", padx=12, pady=(10, 0))
        tk.Label(bar, text="VIEW", font=F.h, fg=MUTED, bg=SURFACE).pack(side="left", padx=(0, 8))
        Segmented(bar, [("detect", "Detection"), ("raw", "Raw"), ("mask", "Difference"),
                        ("edges", "Edges")], m.live_view, self._pick_view).pack(side="left")
        self.b_pause = FlatButton(bar, "Pause", self._toggle_pause, font=F.small, padx=10, pady=5)
        self.b_pause.pack(side="right")
        FlatButton(bar, "Snapshot", self._snapshot, font=F.small, padx=10, pady=5).pack(side="right", padx=(0, 6))
        save = tk.Frame(bar, bg=SURFACE)
        save.pack(side="right", padx=(0, 16))
        tk.Label(save, text="SAVE", font=F.h, fg=MUTED, bg=SURFACE).pack(side="left", padx=(0, 8))
        Segmented(save, [("off", "Off"), ("fails", "Fails"), ("all", "All")], s.save_images,
                  lambda v: self._set("save_images", v)).pack(side="left")

        tune = tk.Frame(t, bg=SURFACE)
        tune.pack(fill="x", padx=12, pady=(12, 12))
        fields = [("EXPOSURE", "exposure_us", 20, 200000, 100, "µs", True),
                  ("GAIN", "gain_db", 0, 48, 1, "", True),
                  ("CAPTURE DELAY", "camera_delay_ms", 0, 1000, 5, "ms", False),
                  ("SENSITIVITY", "vision_sensitivity", 1, 100, 5, "", False),
                  ("MIN DEFECT", "vision_min_area", 5, 5000, 5, "px", False)]
        for i, (label, key, lo, hi, step, unit, apply) in enumerate(fields):
            tune.columnconfigure(i, weight=1)
            cell = tk.Frame(tune, bg=SURFACE)
            cell.grid(row=0, column=i, sticky="w")
            tk.Label(cell, text=label, font=F.h, fg=MUTED, bg=SURFACE).pack(anchor="w")
            Stepper(cell, getattr(s, key), lo, hi, step, unit,
                    lambda v, k=key, a=apply: self._set_cam(k, v, a),
                    entry_width=6, unit_width=2).pack(anchor="w", pady=(4, 0))

    def _pick_source(self, key):
        s = self.ctl.settings
        if key == "folder":
            path = filedialog.askdirectory(title="Choose a folder of can images",
                                           initialdir=s.image_folder or str(DATA_DIR.parent))
            if not path:
                return False
            s.image_folder = path
        self.ctl.cammgr.switch_source(key)
        self._shown_live = -1
        self._schedule_save()

    def _pick_view(self, key):
        self.ctl.cammgr.live_view = key

    def _toggle_pause(self):
        m = self.ctl.cammgr
        m.live_paused = not m.live_paused
        self.b_pause.config(text="Resume" if m.live_paused else "Pause")

    def _snapshot(self):
        path = self.ctl.cammgr.snapshot()
        if path:
            self.ctl.log("INFO", f"Snapshot saved: {path}")
        else:
            self.ctl.log("WARN", "Snapshot failed - no camera frame")

    def _teach(self):
        if not self.ctl.cammgr.teach():
            self.ctl.log("WARN", "No camera frame to teach from")
        self._paint_ref()

    def _clear_ref(self):
        self.ctl.cammgr.clear_reference()
        self._paint_ref()

    def _paint_ref(self):
        v = self.ctl.cammgr.vision
        if v.reference is None:
            self.ref_lbl.config(text="Not taught - detecting by local contrast. Put a GOOD can "
                                     "under the camera and press Teach for best results on real cans.",
                                fg=MUTED)
        else:
            self.ref_lbl.config(text=f"Taught at {datetime.fromtimestamp(v.reference_time):%H:%M:%S}"
                                     f" - every can is compared to it.", fg=GREEN)

    def _set_cam(self, key, value, apply):
        self.ctl.set_setting(key, value)
        if apply:
            self.ctl.cammgr.apply_settings()
        self._schedule_save()

    def _camera_tick(self):
        self._cam_job = None
        m = self.ctl.cammgr
        if m is None or not self._compact:
            return
        if m.ok:
            res = f"{m.resolution[0]}×{m.resolution[1]}" if m.resolution else ""
            self.cam_state_dot.config(fg=GREEN)
            self.cam_state_txt.config(text=f"{m.info}   ·   {res}   ·   {m.fps:.1f} fps", fg=TEXT)
        else:
            self.cam_state_dot.config(fg=AMBER if m.status.startswith("Opening") else RED)
            self.cam_state_txt.config(text=m.status, fg=MUTED)

        cv = self.view_cv
        cw, ch = cv.winfo_width(), cv.winfo_height()
        live = m.live_frame()
        if m.ok and live is not None and cw > 20 and ch > 20:
            if live[2] != self._shown_live or m.live_paused:
                self._shown_live = live[2]
                img = fit(live[0], cw - 4, ch - 4)
                self._live_photo = to_photo(img)
                cv.delete("all")
                cv.create_image(cw // 2, ch // 2, image=self._live_photo)
                x1 = (cw + img.shape[1]) // 2 - 10
                y0 = (ch - img.shape[0]) // 2 + 10
                badge, color = ("PAUSED", AMBER) if m.live_paused else ("● LIVE", RED)
                cv.create_text(x1, y0, anchor="ne", text=f"{badge}   {m.fps:.0f} fps",
                               fill=color, font=F.chip)
        elif cw > 20:
            cv.delete("all")
            cv.create_text(cw // 2, ch // 2, text=m.status, fill=MUTED, font=F.body,
                           width=max(100, cw - 60), justify="center")
            self._shown_live = -1

        last = m.last
        if last is not None and m.last_seq != self._shown_last:
            self._shown_last = m.last_seq
            r = last["result"]
            self._last_photo = to_photo(fit(last["image"], 232, 174))
            self.last_cv.delete("all")
            self.last_cv.create_image(118, 89, image=self._last_photo)
            self.last_big.config(text=f"Can #{last['can_id']}   " + ("PASS" if r.passed else "FAIL"),
                                 fg=GREEN if r.passed else RED)
            saved = f"Saved: {Path(last['path']).name}" if last["path"] else "Not saved (see Save setting)"
            self.last_info.config(text=f"{r.summary()}\n{r.confidence * 100:.0f}% confidence  ·  "
                                       f"{r.ms:.0f} ms  ·  {r.method}\n{saved}\nClick the image to enlarge.")
        self._cam_job = self.root.after(66, self._camera_tick)

    def _open_row_image(self, _=None):
        iid = self.tree.focus()
        r = self.results.get(int(iid)) if iid else None
        if r is None or not r.image:
            return
        p = Path(r.image)
        annotated = p.parent / "annotated" / p.name
        self._show_image(annotated if annotated.exists() else p,
                         f"Can #{r.can_id} - " + ("PASS" if r.passed else r.defect))

    def _open_last_image(self):
        last = self.ctl.cammgr.last if self.ctl.cammgr else None
        if last is not None:
            self._show_image(Path(last["path"]) if last["path"] else None,
                             f"Can #{last['can_id']}", img=last["image"])

    def _show_image(self, path, title, img=None):
        if cv2 is None:
            return
        if img is None:
            if path is None or not path.exists():
                messagebox.showinfo("Image", f"Image not found (it may still be saving):\n{path}")
                return
            img = cv2.imread(str(path))
            if img is None:
                return
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=BG)
        w = min(1100, win.winfo_screenwidth() - 100)
        h = min(820, win.winfo_screenheight() - 160)
        photo = to_photo(fit(img, w, h))
        lbl = tk.Label(win, image=photo, bg=BG)
        lbl.image = photo
        lbl.pack(padx=14, pady=(14, 6))
        tk.Label(win, text=str(path) if path else "(not saved to disk)", font=F.small,
                 fg=MUTED, bg=BG).pack(pady=(0, 12))
        win.bind("<Escape>", lambda e: win.destroy())

    # ----------------------------------------------------------- UI actions
    def _pick_inspector(self, name):
        self.ctl.set_inspector(name)
        self._paint_inspectors()
        self._schedule_save()

    def _set_rate(self, v):
        self.ctl.set_defect_rate(v)
        self._schedule_save()

    def _set(self, key, value):
        self.ctl.set_setting(key, value)
        self._update_burst_hint()
        self._schedule_save()

    def _apply_ip(self):
        if self.ctl.set_camera_ip(self.cam_ip.get()):
            self._schedule_save()
        else:
            self.cam_ip.set(self.ctl.settings.camera_ip)

    def _schedule_save(self):
        if self._save_job:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(700, self._save_now)

    def _save_now(self):
        self._save_job = None
        if self.ctl.save_settings():
            self.saved_lbl.config(text=f"Saved to config.json at {datetime.now():%H:%M:%S}", fg=MUTED)

    def _update_burst_hint(self):
        s = self.ctl.settings
        self.burst_hint.config(
            text=f"Burst length {s.burst_ms} ms - cans must be at least\n"
                 f"{s.burst_ms} ms apart to be rejected individually.")

    def _override_changed(self, on):
        if not self.ctl.set_manual_override(on):
            self.override.set(False)

    def _clear(self):
        if self.results and not messagebox.askyesno(
                "Clear session", "Clear this session's results and statistics?\n"
                                 "(Files in data/ are kept.)"):
            return
        self.results.clear()
        self.recent.clear()
        self.n = self.npass = self.nfail = self.nfired = self.nescaped = 0
        self.sum_ms = 0.0
        self.defects.clear()
        self.session_start = datetime.now()
        self.last_id = None
        self.tree.delete(*self.tree.get_children())
        self.timeline.clear()
        self.bars.set(self.defects, 0)
        self.hero_accent.config(bg=DIM)
        self.hero_big.config(text="—", fg=DIM)
        self.hero_detail.config(text="Waiting for the next can")
        self.hero_meta.config(text=" ")
        self._update_kpis()

    def _export(self):
        if not self.results:
            messagebox.showinfo("Export", "No results in this session yet.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile=f"session_{self.session_start:%Y%m%d_%H%M}.csv")
        if not path:
            return
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(CSV_HEADER)
            for r in self.results.values():
                w.writerow(result_row(r))
        self.ctl.log("INFO", f"Exported {len(self.results)} results to {path}")

    # -------------------------------------------------------- event handling
    def _paint_status(self, st):
        """Plain-language reasons for the current stack light state."""
        if st["faults"]:
            items = [(RED, f) for f in st["faults"][:3]]
            if len(st["faults"]) > 3:
                items.append((RED, f"+{len(st['faults']) - 3} more - see Event log"))
        elif st["warning"]:
            items = [(AMBER, f"{st['warning_reason']} - check upstream, then Acknowledge")]
        elif st["running"]:
            items = [(GREEN, "All systems OK - inspecting")]
        else:
            items = [(GREEN, "All systems OK - press Start")]
        if items == self._status_items:
            return
        self._status_items = items
        for w in self.status_box.winfo_children():
            w.destroy()
        for color, text in items:
            row = tk.Frame(self.status_box, bg=SURFACE)
            row.pack(fill="x", pady=1)
            tk.Label(row, text="●", font=F.small, fg=color, bg=SURFACE).pack(side="left", anchor="n")
            tk.Label(row, text=text, font=F.small, fg=TEXT, bg=SURFACE, justify="left",
                     anchor="w", wraplength=210).pack(side="left", fill="x", padx=(4, 0))

    def _tick(self):
        for _ in range(300):
            try:
                kind, payload = self.events.get_nowait()
            except Exception:
                break
            if kind == "result":
                self._on_result(payload)
            elif kind == "reject":
                self._on_reject(*payload)
            elif kind == "sensor":
                self._draw_sensor(payload)
            elif kind == "log":
                self._append_log(*payload)
        self._refresh()
        self.root.after(100, self._tick)

    @staticmethod
    def _tag(r):
        if r.passed:
            return "pass"
        return "escaped" if r.escaped else "fail"

    def _on_result(self, r: InspectionResult):
        self.results[r.can_id] = r
        self.recent.append(time.monotonic())
        self.n += 1
        self.sum_ms += r.inspect_ms
        if r.passed:
            self.npass += 1
        else:
            self.nfail += 1
            self.defects[r.defect] += 1
            if r.escaped:
                self.nescaped += 1
        self.last_id = r.can_id
        self.tree.insert("", 0, iid=str(r.can_id), tags=(self._tag(r),), values=(
            r.can_id, r.timestamp.strftime("%H:%M:%S"),
            "✓ PASS" if r.passed else "✕ FAIL",
            r.defect or "—", f"{r.confidence * 100:.0f}%", f"{r.inspect_ms:.0f} ms",
            REJECT_LABEL[r.reject], "\u25A3" if r.image else ""))
        kids = self.tree.get_children()
        if len(kids) > MAX_TABLE_ROWS:
            self.tree.delete(*kids[MAX_TABLE_ROWS:])
        self.timeline.push(r)
        self._paint_hero(r)
        self._update_kpis()
        self.bars.set(self.defects, self.n)

    def _on_reject(self, can_id, status):
        r = self.results.get(can_id)
        if r is None:
            return
        r.reject = status
        if status == FIRED:
            self.nfired += 1
        elif status in ESCAPED:
            self.nescaped += 1
        iid = str(can_id)
        if self.tree.exists(iid):
            self.tree.set(iid, "reject", REJECT_LABEL[status])
            self.tree.item(iid, tags=(self._tag(r),))
        self.timeline.redraw()
        if can_id == self.last_id:
            self._paint_hero(r)
        self._update_kpis()

    def _paint_hero(self, r):
        if r.passed:
            text, color = "PASS", GREEN
        elif r.reject == PENDING:
            text, color = "REJECTING…", RED
        elif r.reject == FIRED:
            text, color = "REJECTED", RED
        else:
            text, color = "NOT REJECTED", ORANGE
        self.hero_accent.config(bg=color)
        self.hero_big.config(text=text, fg=color)
        what = "no defect" if r.passed else r.defect
        self.hero_detail.config(text=f"Can #{r.can_id}   ·   {what}   ·   "
                                     f"{r.confidence * 100:.0f}% confidence")
        meta = f"{r.timestamp:%H:%M:%S}  ·  inspected in {r.inspect_ms:.0f} ms  ·  {r.inspector}"
        if not r.passed and r.reject not in (PENDING, FIRED):
            reason = {MISSED: "air fired too late", DISABLED: "rejection is turned off",
                      LOCKED: "rejector locked out"}.get(r.reject, "")
            meta += f"  ·  {reason}"
        self.hero_meta.config(text=meta)

    def _update_kpis(self):
        now = time.monotonic()
        while self.recent and now - self.recent[0] > 60:
            self.recent.popleft()
        n = self.n
        self.kpi["Inspected"].set(n, f"since {self.session_start:%H:%M}")
        self.kpi["Passed"].set(self.npass, f"{self.npass / n * 100:.1f}% of cans" if n else " ")
        if self.nescaped:
            self.kpi["Rejected"].set(self.nfail, f"⚠ {self.nescaped} not rejected",
                                     sub_color=ORANGE)
        else:
            self.kpi["Rejected"].set(self.nfail, f"{self.nfired} blown off" if self.nfail else " ")
        if n:
            y = self.npass / n * 100
            self.kpi["Yield"].set(f"{y:.1f}%", f"{n - self.npass} defective",
                                  color=GREEN if y >= 95 else AMBER if y >= 85 else RED)
        else:
            self.kpi["Yield"].set("—")
        self.kpi["Cans / min"].set(len(self.recent), "last 60 s")
        self.kpi["Inspect"].set(f"{self.sum_ms / n:.0f}" if n else "—",
                                     "ms average" if n else " ")
        self.table_info.config(text=f"{n} cans this session  ·  newest first  ·  "
                                    f"full record saved in data/")

    def _draw_sensor(self, active):
        cv = self.sensor_cv
        cv.delete("all")
        if active:
            cv.create_oval(2, 2, 74, 74, fill=blend(CYAN, SURFACE, 0.82), outline="")
            cv.create_oval(12, 12, 64, 64, fill=blend(CYAN, SURFACE, 0.6), outline="")
            cv.create_oval(22, 22, 54, 54, fill=CYAN, outline="")
            self.sensor_lbl.config(text="CAN PRESENT", fg=CYAN)
        else:
            cv.create_oval(22, 22, 54, 54, fill=SURFACE2, outline=BORDER, width=2)
            self.sensor_lbl.config(text="CLEAR", fg=MUTED)

    def _append_log(self, level, msg, ts):
        t = self.log_txt
        t.config(state="normal")
        t.insert("end", ts.strftime("%H:%M:%S  "), "ts")
        t.insert("end", f"{level:<6}{msg}\n", level)
        lines = int(t.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            t.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        t.see("end")
        t.config(state="disabled")
        if level in ("ERROR", "ALARM"):
            self.nb.tab(self.log_tab, text="Event log  ●")

    # ------------------------------------------------------- periodic refresh
    def _refresh(self):
        st = self.ctl.status()
        faults = st["faults"]
        if st["estop"]:
            pill = ("■  E-STOP", RED, "#ffffff")
        elif faults:
            first = faults[0] if len(faults[0]) <= 34 else faults[0][:33].rstrip(" -:") + "…"
            more = f"   (+{len(faults) - 1})" if len(faults) > 1 else ""
            pill = (f"■  FAULT  ·  {first}{more}", RED, "#ffffff")
        elif st["warning"]:
            pill = (f"▲  WARNING  ·  {st['warning_reason']}", AMBER, "#3a2300")
        elif st["running"]:
            pill = ("●  RUNNING", GREEN, "#052e14")
        elif st["manual"]:
            pill = ("◆  MANUAL OVERRIDE", BLUE, "#ffffff")
        else:
            pill = ("■  STOPPED  ·  ready", RAISED, TEXT)
        self.state_pill.config(text=pill[0], bg=pill[1], fg=pill[2])

        self.chip_io.set("SIMULATION" if st["sim"] else "HARDWARE", AMBER if st["sim"] else GREEN)
        self.chip_sensor.set(st["sensor_mode"], CYAN if st["sensor"] else GREEN)
        if self.cam_ip.get() != st["cam_ip"] and self.root.focus_get() is not self.cam_ip_entry:
            self.cam_ip.set(st["cam_ip"])                # camera found at a new IP
        src = st["cam_source"]
        name = st["cam_ip"] if src == "gige" else SOURCES.get(src, src)
        if st["cam_ok"]:
            self.chip_cam.set(f"{name}  \u00B7  {st['cam_fps']:.0f} fps", GREEN)
        else:
            self.chip_cam.set(name, RED if self.ctl.cammgr else DIM)

        now = datetime.now()
        self.clock.config(text=now.strftime("%H:%M:%S"))
        up = int(st["uptime"])
        self.uptime.config(text=f"{now:%a %b %d}  ·  up {up // 3600}:{up % 3600 // 60:02d}:{up % 60:02d}")

        self.tower.set_state(st["lights"], clickable=st["manual"])
        self.override.set(st["manual"])
        if st["valve"]:
            self.valve_dot.config(fg=RED)
            self.valve_txt.config(text="AIR ON", fg=RED)
        elif st["locked"]:
            self.valve_dot.config(fg=ORANGE)
            self.valve_txt.config(text="LOCKED OUT", fg=ORANGE)
        else:
            self.valve_dot.config(fg=DIM)
            self.valve_txt.config(text="CLOSED", fg=MUTED)

        if st["cam_online"] is True:
            self.cam_dot.config(fg=GREEN)
            self.cam_txt.config(text=f"Online  ·  {st['cam_ms']:.0f} ms", fg=TEXT)
        elif st["cam_online"] is False:
            self.cam_dot.config(fg=RED)
            self.cam_txt.config(text="Not reachable", fg=RED)
        elif not st["cam_ping"]:
            self.cam_dot.config(fg=DIM)
            self.cam_txt.config(text="Unknown - ping not installed", fg=MUTED)
        else:
            self.cam_dot.config(fg=DIM)
            self.cam_txt.config(text="Checking…", fg=MUTED)

        running, estop = st["running"], st["estop"]
        self.b_start.set_enabled(not running and not estop)
        self.b_stop.set_enabled(running)
        self.b_ack.set_enabled(st["warning"])
        self._paint_status(st)
        self.b_sim.set_enabled(running)
        self.b_burst.set_enabled(not running and not estop)

        if self.recent and time.monotonic() - self.recent[0] > 60:
            self._update_kpis()
        elif not self.recent and self.n:
            self.kpi["Cans / min"].set(0, "last 60 s")
