"""
JARVIS HUD — always-on-top floating panel.

Uses macOS 'floatingPanel' window style so it floats above Stage Manager
and never gets hidden when you switch apps.

Left-click ✕  → hide    |    Right-click ✕  → quit    |    ⌘Q → quit
"""

from __future__ import annotations

import math
import random
import tkinter as tk
from typing import Callable, Optional

# ── Palette ───────────────────────────────────────────────────────────────────
BG        = "#0f1923"      # dark navy
BORDER    = "#38bdf8"      # bright sky blue — visible on any background
BG_HEADER = "#0a1520"
BLUE      = "#38bdf8"
BLUE_DIM  = "#0c2236"
TEXT      = "#e2f0fb"
TEXT_DIM  = "#2a5070"
DIM_LINE  = "#1a3040"
GREEN     = "#34d399"
AMBER     = "#fbbf24"

STATUS_COLOR = {
    "idle":      "#1e4060",
    "listening": GREEN,
    "thinking":  AMBER,
    "speaking":  BLUE,
}
STATUS_LABEL = {
    "idle":      "ready",
    "listening": "listening…",
    "thinking":  "thinking…",
    "speaking":  "speaking",
}

W, H    = 380, 200
RADIUS  = 12

HOLO_W  = 76
HOLO_CX = 38 + 10
HOLO_CY = 115

RINGS = [(20, 0.32, 0), (30, 0.52, 1), (38, 0.20, 2)]

RING_SPEEDS = {
    "idle":      [ 0.007, -0.005,  0.004],
    "thinking":  [ 0.028, -0.020,  0.014],
    "listening": [ 0.013, -0.009,  0.007],
    "speaking":  [ 0.020, -0.014,  0.010],
}


class JARVISWindow(tk.Tk):

    def __init__(
        self,
        on_input:  Callable[[str], None],
        on_close:  Optional[Callable] = None,
        on_quit:   Optional[Callable] = None,
    ) -> None:
        super().__init__()
        self._on_input = on_input
        self._on_close = on_close or self.hide
        self._on_quit  = on_quit  or self.destroy

        self._state    = "idle"
        self._response = ""
        self._phase    = 0.0
        self._visible  = True       # start visible
        self._drag_ox  = 0
        self._drag_oy  = 0

        self._ring_phases = [0.0, 2.1, 4.2]
        self._particles   = [self._new_particle() for _ in range(16)]
        for p in self._particles:
            p["life"] = random.uniform(0, p["max_life"])

        # ── Window setup ──────────────────────────────────────────────────────
        self.overrideredirect(True)
        self.wm_attributes("-topmost", True)
        self.wm_attributes("-alpha", 0.97)
        self.configure(bg=BG)

        self.update_idletasks()
        self._place_top_right()

        # ── Canvas ────────────────────────────────────────────────────────────
        self.cv = tk.Canvas(self, width=W, height=H, bg=BG, highlightthickness=0)
        self.cv.place(x=0, y=0)

        # ── Input box ─────────────────────────────────────────────────────────
        self._ivar  = tk.StringVar()
        self._entry = tk.Entry(
            self,
            textvariable=self._ivar,
            bg=BLUE_DIM,
            fg=TEXT,
            insertbackground=BLUE,
            relief="flat", bd=0,
            font=("SF Pro Text", 12),
            highlightthickness=1,
            highlightbackground="#1a4060",
            highlightcolor=BLUE,
        )
        self._entry.place(x=12, y=H - 42, width=W - 48, height=28)
        self._entry.bind("<Return>", self._submit)

        self.bind_all("<Command-q>", lambda _: self._on_quit())
        self.cv.bind("<ButtonPress-1>",  self._press)
        self.cv.bind("<B1-Motion>",      self._drag)
        self.cv.bind("<ButtonPress-2>",  self._right_press)
        self.cv.bind("<ButtonPress-3>",  self._right_press)

        self._draw()
        self._tick()
        # Start visible — don't withdraw
        self.deiconify()
        self._entry.focus_set()

        # Float above Stage Manager (all spaces)
        self._apply_macos_behavior()
        self._stay_on_top()

    # ── Particles ─────────────────────────────────────────────────────────────

    def _new_particle(self) -> dict:
        angle = random.uniform(0, 2 * math.pi)
        speed = random.uniform(0.12, 0.65)
        return {
            "x": HOLO_CX + random.uniform(-4, 4),
            "y": HOLO_CY + random.uniform(-4, 4),
            "vx": math.cos(angle) * speed,
            "vy": math.sin(angle) * speed * 0.45,
            "life": 0,
            "max_life": random.uniform(40, 100),
            "size": random.uniform(0.8, 1.8),
        }

    # ── Visibility ────────────────────────────────────────────────────────────

    def toggle_visibility(self) -> None:
        self.hide() if self._visible else self.show()

    def show(self) -> None:
        self._visible = True
        self.deiconify()
        self.lift()
        self.focus_force()
        self._entry.focus_set()

    def hide(self) -> None:
        self._visible = False
        self.withdraw()

    def _apply_macos_behavior(self) -> None:
        """Set NSWindow collection behavior so it floats above Stage Manager."""
        try:
            from AppKit import NSApp  # pyobjc-framework-Cocoa
            self.update()
            for win in NSApp.windows():
                # CanJoinAllSpaces=1 | Stationary=16 → visible on every space/group
                # (NOT Transient=8 which would HIDE the window from Stage Manager)
                win.setCollectionBehavior_(1 | 16)
                win.setLevel_(25)         # NSStatusWindowLevel — above Stage Manager
            NSApp.activateIgnoringOtherApps_(True)
        except Exception as e:
            print(f"PyObjC: {e}")

    def _stay_on_top(self) -> None:
        """Re-assert topmost every 1.5 s so macOS can't bury us."""
        if self._visible:
            self.lift()
            self.wm_attributes("-topmost", True)
        self.after(1500, self._stay_on_top)

    # ── State & content ───────────────────────────────────────────────────────

    def set_state(self, state: str) -> None:
        self._state = state

    def start_jarvis_message(self) -> None:
        self._response = ""

    def add_jarvis_chunk(self, chunk: str) -> None:
        self._response += chunk

    def add_user_message(self, text: str) -> None:
        pass

    def clear_chat(self) -> None:
        self._response = ""

    def safe_call(self, fn: Callable, *args) -> None:
        try:
            self.after(0, lambda: self._safe_run(fn, args))
        except Exception:
            pass

    def _safe_run(self, fn: Callable, args: tuple) -> None:
        try:
            fn(*args)
        except Exception:
            pass

    # ── Animation ─────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        sp = RING_SPEEDS.get(self._state, RING_SPEEDS["idle"])
        for i in range(3):
            self._ring_phases[i] += sp[i]
        self._phase += 0.05
        for p in self._particles:
            p["x"] += p["vx"]
            p["y"] += p["vy"]
            p["life"] += 1
            if p["life"] >= p["max_life"]:
                p.update(self._new_particle())
        self._draw()
        self.after(40, self._tick)

    # ── Drawing ───────────────────────────────────────────────────────────────

    def _draw(self) -> None:
        cv  = self.cv
        sc  = STATUS_COLOR[self._state]
        cv.delete("all")

        # Solid background — fill entire canvas first so nothing shows through
        cv.create_rectangle(0, 0, W, H, fill=BG, outline="")

        # Rounded card with bright border (2px so it shows on any background)
        self._rrect(1, 1, W - 1, H - 1, RADIUS, fill=BG, outline=BORDER, width=2)

        # Extra glow border when active
        if self._state != "idle":
            self._rrect(0, 0, W, H, RADIUS + 1, fill="", outline=sc, width=1)

        # ── Header ────────────────────────────────────────────────────────────
        self._rrect(1, 1, W - 1, 36, RADIUS, fill=BG_HEADER, outline="")
        cv.create_rectangle(1, 22, W - 1, 36, fill=BG_HEADER, outline="")
        cv.create_line(1, 36, W - 1, 36, fill=BORDER, width=1)

        cv.create_text(
            14, 18, text="◈  JARVIS",
            anchor="w", fill=BLUE, font=("SF Pro Display", 11, "bold"),
        )
        # Status dot + label
        cv.create_oval(W - 108, 11, W - 98, 25, fill=sc, outline="")
        cv.create_text(
            W - 94, 18, text=STATUS_LABEL.get(self._state, ""),
            anchor="w", fill=sc, font=("SF Pro Display", 10),
        )
        cv.create_text(
            W - 10, 12, text="✕", anchor="ne",
            fill=TEXT_DIM, font=("SF Pro Display", 11), tags="close",
        )

        # ── Content area ──────────────────────────────────────────────────────
        # Hologram column
        self._draw_hologram(sc)

        # Divider
        cv.create_line(HOLO_W + 14, 42, HOLO_W + 14, H - 52, fill=DIM_LINE, width=1)

        # Response text
        disp  = self._response or "What can I do for you, Mr. Roe?"
        color = TEXT if self._response else TEXT_DIM
        if len(disp) > 130:
            disp = "…" + disp[-128:]
        cv.create_text(
            HOLO_W + 22, 50,
            text=disp, anchor="nw",
            fill=color, font=("SF Pro Text", 11),
            width=W - HOLO_W - 30,
        )

        # ── Input row ─────────────────────────────────────────────────────────
        cv.create_line(1, H - 52, W - 1, H - 52, fill=BORDER, width=1)
        cv.create_text(
            W - 16, H - 28, text="↵",
            anchor="center", fill=BLUE,
            font=("SF Pro Display", 15), tags="send",
        )

    def _draw_hologram(self, sc: str) -> None:
        cv  = self.cv
        cx, cy = HOLO_CX, HOLO_CY

        # Particles
        for p in self._particles:
            lr  = p["life"] / p["max_life"]
            a   = 1.0 - abs(lr - 0.5) * 2.0
            dx  = p["x"] - cx
            dy  = (p["y"] - cy) / 0.5
            d   = math.sqrt(dx * dx + dy * dy)
            if d > 42:
                continue
            bright = a * max(0.0, 1.0 - d / 42)
            if bright < 0.05:
                continue
            g  = min(255, int(189 * bright))
            b  = min(255, int(248 * bright))
            sz = p["size"] * (0.3 + 0.7 * bright)
            cv.create_oval(
                p["x"] - sz, p["y"] - sz, p["x"] + sz, p["y"] + sz,
                fill=f"#00{g:02x}{b:02x}", outline="",
            )

        # Rings
        for r, tilt, pi in RINGS:
            self._draw_ring(cx, cy, r, tilt, self._ring_phases[pi], sc)

        # Core orb
        pulse = 0.65 + 0.35 * math.sin(self._phase * 2.5)
        orb_r = int(3 + 2 * pulse)
        cv.create_oval(cx - orb_r, cy - orb_r, cx + orb_r, cy + orb_r, fill=sc, outline="")
        cv.create_oval(cx - orb_r - 4, cy - orb_r - 4, cx + orb_r + 4, cy + orb_r + 4,
                       fill="", outline=sc, width=0.7)

    def _draw_ring(self, cx, cy, r, tilt, phase, color) -> None:
        cv = self.cv
        n  = 48
        cr, cg, cb = (int(color[i:i+2], 16) for i in (1, 3, 5)) if color.startswith("#") and len(color)==7 else (56, 189, 248)
        pts = [(cx + r * math.cos(2*math.pi*i/n), cy + r * tilt * math.sin(2*math.pi*i/n)) for i in range(n+1)]
        pn  = (phase % (2*math.pi)) / (2*math.pi)
        for i in range(n):
            x1, y1 = pts[i]
            x2, y2 = pts[i+1]
            d      = min(abs(i/n - pn), 1 - abs(i/n - pn))
            comet  = max(0.0, 1.0 - d * 9)
            depth  = 0.10 + 0.28 * (0.5 + 0.5 * math.sin(2*math.pi*(i+0.5)/n))
            bright = depth + comet * 0.72
            seg    = f"#{min(255,int(cr*bright)):02x}{min(255,int(cg*bright)):02x}{min(255,int(cb*bright)):02x}"
            cv.create_line(x1, y1, x2, y2, fill=seg, width=1.0 + comet * 1.6)

    def _rrect(self, x1, y1, x2, y2, r, **kw) -> None:
        pts = [x1+r,y1, x2-r,y1, x2,y1, x2,y1+r, x2,y2-r, x2,y2, x2-r,y2, x1+r,y2, x1,y2, x1,y2-r, x1,y1+r, x1,y1]
        self.cv.create_polygon(pts, smooth=True, **kw)

    # ── Input ─────────────────────────────────────────────────────────────────

    def _submit(self, _event=None) -> None:
        text = self._ivar.get().strip()
        if text:
            self._ivar.set("")
            self._on_input(text)

    # ── Mouse ─────────────────────────────────────────────────────────────────

    def _press(self, event) -> None:
        for tag, fn in [("close", self._on_close), ("send", self._submit)]:
            for item in self.cv.find_withtag(tag):
                bb = self.cv.bbox(item)
                if bb and bb[0]-8 <= event.x <= bb[2]+8 and bb[1]-8 <= event.y <= bb[3]+8:
                    fn()
                    return
        self._drag_ox = event.x
        self._drag_oy = event.y

    def _right_press(self, event) -> None:
        for item in self.cv.find_withtag("close"):
            bb = self.cv.bbox(item)
            if bb and bb[0]-10 <= event.x <= bb[2]+10 and bb[1]-10 <= event.y <= bb[3]+10:
                self._on_quit()
                return

    def _drag(self, event) -> None:
        x = self.winfo_x() + event.x - self._drag_ox
        y = self.winfo_y() + event.y - self._drag_oy
        self.geometry(f"+{x}+{y}")

    def _place_top_right(self) -> None:
        try:
            import subprocess
            r = subprocess.run(
                ["osascript", "-e",
                 "tell application \"Finder\" to get bounds of window of desktop"],
                capture_output=True, text=True, timeout=2,
            )
            parts = [int(x.strip()) for x in r.stdout.strip().split(",")]
            sw = parts[2]   # right edge = logical screen width
        except Exception:
            sw = self.winfo_screenwidth() or 1440
        if sw < 400:
            sw = 1440
        self.geometry(f"{W}x{H}+{sw - W - 20}+28")
