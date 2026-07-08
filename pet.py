"""opencode-pet: desktop pet overlay (Windows tkinter + GIF sprites)

A transparent, topmost, borderless window showing an animated cat (sprites
from 月薪喵 / desktop_cat, MIT) that reacts to opencode session activity.
State + bubble protocol over stdin:

  {"type":"status","value":"idle|busy|thinking|speaking|tool"}
  {"type":"flash","value":"success|fail|celebrate","duration":1500}
  {"type":"alert","text":"..."}                  persistent attention grab
  {"type":"clear_alert"}
  {"type":"bubble","text":"...","duration":3000} duration 0 = persistent
  {"type":"clear_bubble"}
  {"type":"quit"}

Drawing priority (highest first):
  alert → flash (success/fail/celebrate) → base status (idle/busy/...)
Each state maps to a pre-rendered GIF; frames are blitted via itemconfig
(no per-frame delete+create) for smooth motion.

Interactions:
  Left-drag       move the pet; release with momentum to throw it
                  (gravity + floor bounce + wall bounce + friction)
  Double-click    pet the cat → floating hearts + tiny happy bounce
  Right-click     dismiss alert (does not close the window)

Performance architecture (three-tier frequency separation):
  - Physics  120 Hz (after(8))  — position integration + geometry() only
  - Render    60 Hz (every 2 ticks) — itemconfig/coords updates, no delete
  - GIF frame ~12 Hz (every 10 ticks) — frame index advance
  Physics constants are calibrated in "100ms-step" units; integration scales
  by real elapsed dt so motion is frame-rate independent.
"""

import json
import math
import os
import random
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont

from PIL import Image, ImageSequence, ImageTk

# Windows std streams default to ANSI (GBK) — emoji and CJK would garble.
# Reconfigure to UTF-8 before any IO happens.
if sys.platform == "win32":
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Win32 helpers — apply WS_EX_NOACTIVATE so the pet window never steals
# keyboard focus from the terminal. This is the standard Windows mechanism
# for desktop pets / notifications / on-screen keyboards.
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32

    _GWL_EXSTYLE        = -20
    _WS_EX_NOACTIVATE   = 0x08000000
    _WS_EX_TOOLWINDOW   = 0x00000080

    _SW_HIDE            = 0
    _SW_RESTORE         = 9
    _SWP_NOSIZE         = 0x0001
    _SWP_NOMOVE         = 0x0002
    _SWP_NOZORDER       = 0x0004
    _SWP_NOACTIVATE     = 0x0010
    _SWP_FRAMECHANGED   = 0x0020
    _SWP_SHOWWINDOW     = 0x0040

    _TH32CS_SNAPPROCESS = 0x00000002

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize",              wintypes.DWORD),
            ("cntUsage",            wintypes.DWORD),
            ("th32ProcessID",       wintypes.DWORD),
            ("th32DefaultHeapID",   ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID",        wintypes.DWORD),
            ("cntThreads",          wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase",      ctypes.c_long),
            ("dwFlags",             wintypes.DWORD),
            ("szExeFile",           ctypes.c_wchar * 260),
        ]

    _kernel32 = ctypes.windll.kernel32

    # --- EnumWindows callback (module-level to avoid GC) ---
    # ctypes callbacks MUST be kept alive as long as EnumWindows might use
    # them. A local-scope _ENUMPROC(_cb) gets GC'd mid-enumeration.
    _enum_found = [None]
    _enum_target_pid = [0]

    def _enum_cb_impl(hwnd, _lparam):
        if _user32.IsWindowVisible(hwnd):
            wpid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if wpid.value == _enum_target_pid[0]:
                title = ctypes.create_unicode_buffer(256)
                _user32.GetWindowTextW(hwnd, title, 256)
                if title.value:
                    _enum_found[0] = hwnd
                    return False  # stop enumeration
        return True

    _ENUMPROC_TYPE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _enum_proc = _ENUMPROC_TYPE(_enum_cb_impl)

    def _find_window_by_pid(pid):
        """Find the visible top-level window owned by `pid`. Returns HWND or None."""
        _enum_found[0] = None
        _enum_target_pid[0] = pid
        _user32.EnumWindows(_enum_proc, 0)
        return _enum_found[0]

    def _find_ancestor_window(pid):
        """Walk up the process tree from `pid` and return the HWND of the
        first ancestor that owns a visible top-level window with a title
        (the terminal host: WindowsTerminal.exe, cmd.exe, etc.).

        Returns HWND (int) or None.
        """
        # Build PID → parent-PID map from a process snapshot
        parent_map = {}
        snap = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if snap:
            pe = _PROCESSENTRY32W()
            pe.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            if _kernel32.Process32FirstW(snap, ctypes.byref(pe)):
                while True:
                    parent_map[pe.th32ProcessID] = pe.th32ParentProcessID
                    if not _kernel32.Process32NextW(snap, ctypes.byref(pe)):
                        break
            _kernel32.CloseHandle(snap)

        sys.stderr.write(f"[pet] process tree: walking from pid={pid}\n")
        # Walk up from pid, checking each ancestor for a visible window
        current = pid
        visited = set()
        while current and current not in visited:
            visited.add(current)
            hwnd = _find_window_by_pid(current)
            sys.stderr.write(f"[pet]   pid={current} hwnd={hwnd}\n")
            if hwnd:
                return hwnd
            current = parent_map.get(current)
        return None

    def _bring_window_to_front(hwnd):
        """Activate a window from a non-foreground process.

        Windows blocks SetForegroundWindow unless the caller is already the
        foreground process. We bypass this with AttachThreadInput, which
        temporarily attaches our input thread to the target's, making us
        eligible to call SetForegroundWindow.
        """
        fg_hwnd = _user32.GetForegroundWindow()
        fg_tid = _user32.GetWindowThreadProcessId(fg_hwnd, None)
        target_tid = _user32.GetWindowThreadProcessId(hwnd, None)

        # Restore if minimized
        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, _SW_RESTORE)

        if fg_tid != target_tid:
            _user32.AttachThreadInput(fg_tid, target_tid, True)
        _user32.SetForegroundWindow(hwnd)
        _user32.BringWindowToTop(hwnd)
        if fg_tid != target_tid:
            _user32.AttachThreadInput(fg_tid, target_tid, False)

    def _mark_window_no_activate(hwnd):
        """Mark window so it never gets activated (no keyboard focus).

        WS_EX_NOACTIVATE must be in effect when the window is shown. Setting
        it after the window is already visible has no effect, because Windows
        caches the activation state. So we:
          1. Hide the window (if visible)
          2. Add WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW to extended styles
          3. Re-show via SetWindowPos with SWP_NOACTIVATE | SWP_SHOWWINDOW,
             which forces Windows to re-evaluate the extended style.
        Returns (hwnd, ok).
        """
        if not hwnd:
            return (0, False)
        try:
            # climb to the true top-level window (Tk child windows can nest)
            try:
                _user32.GetParent.restype = wintypes.HWND
                _user32.GetParent.argtypes = [wintypes.HWND]
                top = hwnd
                while True:
                    parent = _user32.GetParent(top)
                    if not parent:
                        break
                    top = parent
                hwnd = top
            except Exception:
                pass

            # 1. hide if visible
            if _user32.IsWindowVisible(hwnd):
                _user32.ShowWindow(hwnd, _SW_HIDE)

            # 2. update extended style
            ex = _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
            ex |= _WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW
            _user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, ex)

            # 3. re-show without activating; FRAMECHANGED flushes the new style
            _user32.SetWindowPos(
                hwnd, 0, 0, 0, 0, 0,
                _SWP_NOSIZE | _SWP_NOMOVE | _SWP_NOZORDER
                | _SWP_NOACTIVATE | _SWP_SHOWWINDOW | _SWP_FRAMECHANGED,
            )
            return (hwnd, True)
        except Exception as e:
            sys.stderr.write(f"[pet] no-activate error: {e}\n")
            return (hwnd, False)

# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

CANVAS_W = 200
CANVAS_H = 220
TRANSPARENT = "#010101"

# Ground offset from the bottom of the screen work area. Lifts the pet above
# the Windows taskbar (typically 40-48 px; 60 gives some breathing room).
FLOOR_MARGIN = 60

# Horizontal gap between pet windows when multiple opencode instances run.
PET_GAP = 20

PET_CX = CANVAS_W // 2
PET_CY = 160

BUBBLE_X = 12
BUBBLE_Y = 10
BUBBLE_W = CANVAS_W - 24
BUBBLE_H = 70

GIF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gifs")

# ---------------------------------------------------------------------------
# Scheduling (three-tier frequency separation)
# ---------------------------------------------------------------------------

PHYSICS_HZ = 120
PHYSICS_INTERVAL = 1.0 / PHYSICS_HZ     # 8.33 ms — physics integration step
RENDER_INTERVAL = 1.0 / 60.0             # 16.7 ms — render throttle (60 fps)
GIF_INTERVAL = 0.080                     # 80 ms — GIF frame advance (12 fps)
STEP_UNIT = 0.1                          # physics constants calibrated per 100ms-step
MAX_STEPS = 3.0                          # clamp dt to avoid jumps after stall

# Idle: waiting is primary; running-right triggers at random intervals
IDLE_WALK_MIN_DELAY = 20.0   # min seconds between walks
IDLE_WALK_MAX_DELAY = 120.0  # max seconds between walks
IDLE_WALK_DURATION  = 1.5    # how long a walk lasts (seconds)
# Busy sequence: entering work (run) → thinking (review) → working (work)
BUSY_RUN_DURATION    = 1.5   # running-right when busy starts
BUSY_REVIEW_DURATION = 1.5   # review after run
# after run+review elapses, busy_work (running-left) takes over
# speaking flag overrides busy_work with running.gif
# celebrate flash duration when busy → idle (waving)
BUSY_CELEBRATE_MS    = 5000

# ---------------------------------------------------------------------------
# State → GIF mapping
# ---------------------------------------------------------------------------

STATE_TO_GIF = {
    # idle: waiting is primary; running-right triggers at random intervals
    "idle":        "waiting.gif",      # fallback
    "idle_wait":   "waiting.gif",
    "idle_walk":   "running-right.gif",
    # busy: run → review → work (running-left), overridden by speaking flag
    "busy":        "review.gif",       # fallback
    "busy_run":    "running-right.gif",
    "busy_review": "review.gif",
    "busy_work":   "running-left.gif",
    "thinking":    "review.gif",
    "speaking":    "running.gif",
    "tool":        "running.gif",
    "moving":      "jumping.gif",
    "success":     "waving.gif",
    "celebrate":   "waving.gif",
    "fail":        "failed.gif",
    "alert":       "waiting.gif",
}

# White-background GIFs are pre-processed offline by preprocess_gifs.py
# (flood-fill from corners → transparent index 255). No runtime keying needed.

BUBBLE_BG   = "#FFFFFF"
BUBBLE_BD   = "#D48516"
TEXT_DARK   = "#1F1F1F"
HEART_PINK  = "#FF6B95"
HEART_LITE  = "#FFB6C1"


# ---------------------------------------------------------------------------
# Instance slot manager — assign each pet a horizontal slot so multiple
# opencode windows don't overlap. Uses a file-based registry in the same dir.
# ---------------------------------------------------------------------------

def _registry_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".instances")


def _read_live_pids():
    """Read the registry and return only alive PIDs (deduplicated)."""
    path = _registry_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            pids = [int(line.strip()) for line in f if line.strip().isdigit()]
    except (FileNotFoundError, ValueError, OSError):
        return []
    # prune dead PIDs (best-effort, Windows-only)
    if sys.platform == "win32":
        pids = [p for p in pids if _pid_alive(p)]
    # dedupe preserving order
    seen = set()
    unique = []
    for p in pids:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def _write_pids(pids):
    path = _registry_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            for p in pids:
                f.write(f"{p}\n")
    except OSError:
        pass


def acquire_slot():
    """Register our PID in the instance file.

    Returns our PID. The actual slot index is computed dynamically by
    compute_slot() so positions re-compact when other pets close.
    """
    my_pid = os.getpid()
    pids = _read_live_pids()
    if my_pid not in pids:
        pids.append(my_pid)
    _write_pids(pids)
    return my_pid


def compute_slot(my_pid):
    """Return the current slot index (0-based) for my_pid.

    Slot = position of my_pid in the live-PID list, ordered by **startup
    time** (the order PIDs were appended to the registry file). The oldest
    pet (earliest startup) is slot 0 = rightmost; newer pets get higher
    slots and appear further left.
    """
    pids = _read_live_pids()   # already in startup order (append order)
    try:
        return pids.index(my_pid)
    except ValueError:
        # we're not in the file (shouldn't happen) — re-register
        pids = _read_live_pids()
        if my_pid not in pids:
            pids.append(my_pid)
            _write_pids(pids)
        return pids.index(my_pid)


def release_slot(token):
    """Remove our PID from the registry file (best-effort)."""
    my_pid = token
    pids = [p for p in _read_live_pids() if p != my_pid]
    _write_pids(pids)


def _pid_alive(pid):
    """Check if a PID is still running on Windows (best-effort)."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            if not ok:
                return False
            return code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        return True  # assume alive to avoid clobbering others


class PetWindow:
    def __init__(self):
        # Register our PID. Actual slot is computed dynamically so positions
        # re-compact (shift right) when other pets close.
        self.slot_token = acquire_slot()
        self.slot = compute_slot(self.slot_token)

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.wm_attributes("-transparentcolor", TRANSPARENT)
        self.root.config(bg=TRANSPARENT)

        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        # Each instance shifts left by (CANVAS_W + PET_GAP) pixels.
        slot_offset = self.slot * (CANVAS_W + PET_GAP)
        # Final resting position (home). Pet does NOT spawn here — it is
        # thrown in from off-screen right and walks to home after landing.
        self.home_x = sw - CANVAS_W - 60 - slot_offset
        # Lift the pet above the Windows taskbar (typically 40-48 px tall).
        self.home_y = sh - CANVAS_H - FLOOR_MARGIN
        # Spawn off-screen right at ground level, then throw it leftward
        # with an upward velocity so it arcs in (physics engine handles the
        # trajectory + bounces; _maybe_go_home walks it to home_x after landing).
        self.x = sw + 40
        self.y = self.home_y
        self.root.geometry(f"{CANVAS_W}x{CANVAS_H}+{int(self.x)}+{int(self.y)}")

        # Mark the window as "no-activate" so it NEVER steals keyboard focus.
        # This must happen AFTER geometry is set but BEFORE mainloop() runs
        # (which is when the window is first shown). WS_EX_NOACTIVATE only
        # takes effect if it's set before the first ShowWindow call.
        if sys.platform == "win32":
            self.root.update_idletasks()   # force Tk to finalize the HWND
            top_hwnd = self.root.winfo_id()
            hwnd, ok = _mark_window_no_activate(top_hwnd)
            self._top_hwnd = hwnd
            sys.stderr.write(f"[pet] no-activate hwnd={hwnd} ok={ok}\n")
            sys.stderr.flush()

        self.canvas = tk.Canvas(
            self.root, width=CANVAS_W, height=CANVAS_H,
            bg=TRANSPARENT, highlightthickness=0, bd=0,
        )
        self.canvas.pack()

        try:
            self.bubble_font = tkfont.Font(family="Segoe UI", size=10, weight="normal")
        except Exception:
            self.bubble_font = None

        # Bindings
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        # Single click (detected on release if not dragged) → hearts.
        # Double click → focus the parent terminal window.
        self.canvas.bind("<ButtonPress-3>", lambda _e: self.clear_alert())

        # ---- Load sprite frames --------------------------------------
        self.gifs = {}
        self.gif_size = {}
        self._load_gifs()

        # ---- Interaction / drag state --------------------------------
        self.dragging = False
        self.drag_offset = (0, 0)
        # Click detection: track press position + last click time so we can
        # distinguish single-click (hearts) vs double-click (focus terminal)
        # from drag (physics throw) in _on_release.
        self._press_x = 0
        self._press_y = 0
        self._last_click_time = 0.0
        # PID of the opencode process that spawned us (for terminal focus).
        self.term_pid = None
        # Cached terminal window HWND — found once at startup when term_pid
        # arrives, then reused on double-click. Avoids fragile late lookup.
        self.term_hwnd = None

        # ---- Physics (constants in 100ms-step units; dt-scaled at runtime) ----
        # New pet is launched: spawn off-screen right with leftward + upward
        # velocity. Physics engine arcs it in; bounces; _maybe_go_home walks
        # it to home_x after it comes to rest.
        self.vx = -26            # px per step — throw leftward (toward home)
        self.vy = -16            # px per step — initial upward kick for an arc
        self.falling = True      # start airborne so physics runs immediately
        self._drag_hist = []
        self.GRAVITY = 4.0         # px per step² (was 2.2 — heavier fall)
        self.AIR_DRAG = 0.045      # per step — velocity bleed in flight (terminal velocity feel)
        self.BOUNCE = 0.30         # lower → fewer, lower bounces, stops faster
        self.FRICTION = 0.55       # per step — ground friction after landing
        self.FLOOR_MARGIN = FLOOR_MARGIN  # ground offset (taskbar clearance)
        self.PET_HEARTS_MS = 1500
        self.petted_until = 0.0
        self._last_phys_time = time.time()
        self._last_render_time = 0.0
        self._last_gif_time = 0.0

        # ---- State --------------------------------------------------
        self.status = "idle"
        self.flash = None
        self.flash_until = 0.0
        self.alert_active = False
        self.alert_text = ""
        self.bubble_text = ""
        self.bubble_until = 0.0

        self.frame_idx = 0
        # home_x/home_y already set in __init__ (slot-based bottom-right).
        # Do NOT overwrite with self.x — that is the spawn point (off-screen
        # right), not the home target.
        self.moving_home = False   # True while walking back to home corner
        self._busy_since = 0.0     # when busy started (for run→review→work transition)
        # activity channel: "idle" | "thinking" | "speaking" | "tool"
        # driven by message.part.updated (part.type). Overrides idle/busy_work
        # so streaming output is reflected immediately.
        self.activity = "idle"
        self._idle_walk_until = 0.0  # >0 means currently walking (running-right)
        self._idle_next_walk = 0.0   # timestamp when next walk should trigger
        self.tick = 0              # physics tick counter (120 Hz)

        # ---- Render layer: pre-created canvas items (no per-frame delete) ----
        self.pet_item = None       # create_image id, set on first render
        self._last_pet_gif = None
        self._last_pet_frame = -1
        self.heart_items = []      # list of (oval1, oval2, polygon) tuples
        self.bubble_items = []     # list of canvas ids for current bubble
        self._last_bubble_text = "__sentinel__"
        self._bubble_visible = False

    # =====================================================================
    # GIF loading
    # =====================================================================

    def _load_gifs(self):
        for fname in STATE_TO_GIF.values():
            if fname in self.gifs:
                continue
            path = os.path.join(GIF_DIR, fname)
            if not os.path.exists(path):
                sys.stderr.write(f"[pet] missing gif: {path}\n")
                continue
            frames = []
            first_size = None
            with Image.open(path) as im:
                first_size = im.size
                for fr in ImageSequence.Iterator(im):
                    rgba = fr.convert("RGBA")
                    bg = Image.new("RGBA", rgba.size, _hex_to_rgba(TRANSPARENT))
                    bg.alpha_composite(rgba)
                    tk_img = ImageTk.PhotoImage(bg.convert("RGBA"))
                    frames.append(tk_img)
            self.gifs[fname] = frames
            self.gif_size[fname] = first_size
            sys.stderr.write(
                f"[pet] loaded {fname}: {len(frames)} frames @ {first_size}\n"
            )

    def _current_gif_name(self):
        eff = self._effective_state()
        return STATE_TO_GIF.get(eff, "idle.gif")

    # =====================================================================
    # Render layer (itemconfig/coords — no delete+create per frame)
    # =====================================================================

    def _ensure_pet_item(self):
        if self.pet_item is None:
            name = STATE_TO_GIF["idle"]
            frames = self.gifs.get(name)
            if frames:
                self.pet_item = self.canvas.create_image(
                    0, 0, anchor="nw", image=frames[0]
                )
                self._last_pet_gif = name
                self._last_pet_frame = 0

    def _render_pet(self):
        name = self._current_gif_name()
        frames = self.gifs.get(name)
        if not frames or self.pet_item is None:
            return
        w, h = self.gif_size.get(name, (155, 155))
        x = PET_CX - w // 2
        y = PET_CY - h // 2
        idx = self.frame_idx % len(frames)
        # only swap image when GIF or frame actually changes
        if name != self._last_pet_gif or idx != self._last_pet_frame:
            self.canvas.itemconfig(self.pet_item, image=frames[idx])
            self._last_pet_gif = name
            self._last_pet_frame = idx
        self.canvas.coords(self.pet_item, x, y)

    def _render_hearts(self):
        visible = time.time() < self.petted_until
        # lazy-create 3 heart groups (2 ovals + 1 polygon each), hidden by default
        if not self.heart_items:
            for _ in range(3):
                o1 = self.canvas.create_oval(0, 0, 0, 0, fill=HEART_LITE,
                                             outline="", state="hidden")
                o2 = self.canvas.create_oval(0, 0, 0, 0, fill=HEART_LITE,
                                             outline="", state="hidden")
                p = self.canvas.create_polygon(0, 0, 0, 0, 0, 0, fill=HEART_LITE,
                                               outline="", state="hidden")
                self.heart_items.append((o1, o2, p))
            # hearts render above pet
            for o1, o2, p in self.heart_items:
                self.canvas.tag_raise(o1)
                self.canvas.tag_raise(o2)
                self.canvas.tag_raise(p)

        if not visible:
            for o1, o2, p in self.heart_items:
                self.canvas.itemconfig(o1, state="hidden")
                self.canvas.itemconfig(o2, state="hidden")
                self.canvas.itemconfig(p, state="hidden")
            return

        remaining = self.petted_until - time.time()
        total = self.PET_HEARTS_MS / 1000.0
        ratio = max(0.0, min(1.0, remaining / total))
        for i, (o1, o2, p) in enumerate(self.heart_items):
            phase = (self.frame_idx * 0.4 + i * 1.3) % (math.pi * 2)
            base_x = PET_CX + (i - 1) * 18
            rise = (1.0 - ratio) * 50
            hx = base_x + math.sin(phase) * 4
            hy = PET_CY - 70 - rise + i * 5
            r = 4 + ratio * 2
            col = HEART_PINK if i == 1 else HEART_LITE
            self.canvas.coords(o1, hx - r, hy - r * 0.6, hx, hy + r * 0.4)
            self.canvas.coords(o2, hx, hy - r * 0.6, hx + r, hy + r * 0.4)
            self.canvas.coords(p, hx - r * 0.85, hy + r * 0.2,
                               hx + r * 0.85, hy + r * 0.2,
                               hx, hy + r * 1.1)
            self.canvas.itemconfig(o1, state="normal", fill=col)
            self.canvas.itemconfig(o2, state="normal", fill=col)
            self.canvas.itemconfig(p, state="normal", fill=col)

    def _render_bubble(self):
        has_text = bool(self.bubble_text)
        # rebuild only when visibility or text changes
        if has_text == self._bubble_visible and (
                not has_text or self.bubble_text == self._last_bubble_text):
            return
        for item in self.bubble_items:
            self.canvas.delete(item)
        self.bubble_items = []
        self._bubble_visible = has_text
        self._last_bubble_text = self.bubble_text if has_text else "__sentinel__"
        if not has_text:
            return
        c = self.canvas
        r = c.create_rectangle(BUBBLE_X, BUBBLE_Y,
                               BUBBLE_X + BUBBLE_W, BUBBLE_Y + BUBBLE_H,
                               fill=BUBBLE_BG, outline=BUBBLE_BD, width=2)
        p1 = c.create_polygon(PET_CX - 8, BUBBLE_Y + BUBBLE_H,
                              PET_CX + 8, BUBBLE_Y + BUBBLE_H,
                              PET_CX, BUBBLE_Y + BUBBLE_H + 12,
                              fill=BUBBLE_BG, outline="")
        l1 = c.create_line(PET_CX - 8, BUBBLE_Y + BUBBLE_H,
                           PET_CX, BUBBLE_Y + BUBBLE_H + 12,
                           fill=BUBBLE_BD, width=2)
        l2 = c.create_line(PET_CX + 8, BUBBLE_Y + BUBBLE_H,
                           PET_CX, BUBBLE_Y + BUBBLE_H + 12,
                           fill=BUBBLE_BD, width=2)
        t = c.create_text(PET_CX, BUBBLE_Y + BUBBLE_H // 2,
                          text=self.bubble_text,
                          font=self.bubble_font or ("Segoe UI", 10),
                          fill=TEXT_DARK, width=BUBBLE_W - 16,
                          justify="center", anchor="center")
        self.bubble_items = [r, p1, l1, l2, t]
        # bubble renders on top of pet and hearts
        for item in self.bubble_items:
            self.canvas.tag_raise(item)

    # =====================================================================
    # State logic
    # =====================================================================

    def _effective_state(self):
        # Priority order (high → low):
        #   alert > flash > moving > busy-sequence > idle-sequence
        # Within busy: run → review → work, with speaking overriding work only.
        if self.alert_active:
            return "alert"
        if self.flash and time.time() < self.flash_until:
            return self.flash
        if self.moving_home:
            return "moving"
        # busy: strict time-based sequence; activity overrides only the work phase
        if self.status == "busy":
            elapsed = (time.time() - self._busy_since) if self._busy_since else 999
            if elapsed < BUSY_RUN_DURATION:
                return "busy_run"
            if elapsed < BUSY_RUN_DURATION + BUSY_REVIEW_DURATION:
                return "busy_review"
            # past the entrance sequence → work state, but activity overrides
            return self.activity if self.activity != "idle" else "busy_work"
        # idle: waiting is primary; running-right triggers at random intervals
        # BUT if activity is active (streaming), override with the activity GIF
        if self.status == "idle":
            if self.activity != "idle":
                return self.activity
            now = time.time()
            # currently walking?
            if self._idle_walk_until > 0:
                if now < self._idle_walk_until:
                    return "idle_walk"
                # walk ended → clear and schedule next
                self._idle_walk_until = 0.0
                self._idle_next_walk = now + random.uniform(
                    IDLE_WALK_MIN_DELAY, IDLE_WALK_MAX_DELAY
                )
            # need to schedule first walk?
            if self._idle_next_walk == 0.0:
                self._idle_next_walk = now + random.uniform(
                    IDLE_WALK_MIN_DELAY, IDLE_WALK_MAX_DELAY
                )
            # time to start a walk?
            if now >= self._idle_next_walk:
                self._idle_walk_until = now + IDLE_WALK_DURATION
                return "idle_walk"
            return "idle_wait"
        return self.status

    def _expire_layers(self):
        if self.flash and time.time() >= self.flash_until:
            self.flash = None
            self.flash_until = 0
        if self.bubble_text and self.bubble_until and time.time() >= self.bubble_until:
            self.bubble_text = ""
            self.bubble_until = 0

    def set_status(self, v):
        # record when busy started so _effective_state can pick run/review/work
        if v == "busy" and self.status != "busy":
            self._busy_since = time.time()
        # reset idle walk schedule on any transition
        if v != self.status:
            self._idle_walk_until = 0.0
            self._idle_next_walk = 0.0
        self.status = v

    def set_activity(self, v):
        # activity channel — does not touch self.status.
        # _effective_state honors it during idle and busy_work phases.
        # The busy entrance sequence (run → review) always plays uninterrupted.
        self.activity = v if v in ("idle", "thinking", "speaking", "tool") else "idle"

    def flash_value(self, v, duration_ms):
        self.flash = v
        self.flash_until = time.time() + duration_ms / 1000.0

    def set_alert(self, text):
        self.alert_active = True
        self.alert_text = text or ""
        self.jump_to_attention()

    def clear_alert(self):
        self.alert_active = False
        self.alert_text = ""
        # _maybe_go_home will walk it back to bottom-right corner

    def set_bubble(self, text, duration_ms):
        self.bubble_text = text or ""
        self.bubble_until = time.time() + duration_ms / 1000.0 if duration_ms > 0 else 0
        if not self.bubble_text:
            self.bubble_until = 0

    def clear_bubble(self):
        self.bubble_text = ""
        self.bubble_until = 0

    def jump_to_attention(self):
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.x = (sw - CANVAS_W) // 2
        self.y = sh - CANVAS_H - self.FLOOR_MARGIN
        self.root.geometry(f"+{self.x}+{self.y}")

    # =====================================================================
    # Physics & wander (dt-scaled; constants in 100ms-step units)
    # =====================================================================

    def _update_slot_position(self):
        """Recompute our slot index and update home_x.

        Does NOT move the window directly — just sets a new home target.
        The existing _maybe_go_home() physics routine walks the pet there
        smoothly at 7 px/step (jumping.gif while moving).
        """
        new_slot = compute_slot(self.slot_token)
        if new_slot == self.slot:
            return
        self.slot = new_slot
        sw = self.root.winfo_screenwidth()
        slot_offset = self.slot * (CANVAS_W + PET_GAP)
        self.home_x = sw - CANVAS_W - 60 - slot_offset
        # _maybe_go_home will pick up the new home_x on the next physics step
        # and walk the pet there. No instant teleport.

    def _physics_step(self, steps):
        if not self.falling or self.dragging or steps <= 0:
            return
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        floor_y = sh - CANVAS_H - self.FLOOR_MARGIN

        # air drag — velocity bleed, applied each step in flight
        drag = (1.0 - self.AIR_DRAG) ** steps
        self.vx *= drag
        self.vy *= drag

        self.vy += self.GRAVITY * steps
        self.x += self.vx * steps
        self.y += self.vy * steps

        if self.y >= floor_y:
            self.y = floor_y
            if abs(self.vy) > 2.5:
                self.vy = -self.vy * self.BOUNCE
            else:
                self.vy = 0
                # friction applied per step
                self.vx *= (self.FRICTION ** steps)
                if abs(self.vx) < 0.3:
                    self.vx = 0
                    self.falling = False
                    # home stays at bottom-right corner; _maybe_go_home walks there

        if self.y < 0:
            self.y = 0
            self.vy = abs(self.vy) * self.BOUNCE

        if self.x < 0:
            self.x = 0
            self.vx = -self.vx * self.BOUNCE
        # NOTE: no right-edge clamp — allows new pets to be thrown in from
        # off-screen right. After landing, _maybe_go_home walks them to
        # home_x (which is always on-screen).
        # NOTE: geometry() committed in render branch, not here

    def _maybe_go_home(self, steps):
        """Walk back to the fixed home corner (bottom-right, ground level).
        Sets moving_home flag so _effective_state can pick the moving GIF.
        Only active when idle (not dragging, not falling, not alerting)."""
        if self.dragging or self.falling or self.alert_active:
            self.moving_home = False
            return
        dx = self.home_x - self.x
        dy = self.home_y - self.y
        if abs(dx) < 1 and abs(dy) < 1:
            self.moving_home = False
            return
        self.moving_home = True
        speed = 7.0 * steps
        self.x += max(-speed, min(speed, dx))
        self.y += max(-speed, min(speed, dy))

    # =====================================================================
    # Mouse events
    # =====================================================================

    def _on_press(self, e):
        self.dragging = True
        self.falling = False
        self.vx, self.vy = 0, 0
        self.drag_offset = (e.x_root - self.x, e.y_root - self.y)
        self._drag_hist = [(time.time(), self.x, self.y)]
        self._press_x = e.x_root
        self._press_y = e.y_root

    def _on_drag(self, e):
        if not self.dragging:
            return
        self.x = e.x_root - self.drag_offset[0]
        self.y = e.y_root - self.drag_offset[1]
        self.root.geometry(f"+{int(self.x)}+{int(self.y)}")
        now = time.time()
        self._drag_hist.append((now, self.x, self.y))
        cutoff = now - 0.12
        while len(self._drag_hist) > 2 and self._drag_hist[0][0] < cutoff:
            self._drag_hist.pop(0)

    def _on_release(self, e):
        self.dragging = False
        # --- Click vs drag detection ---
        # If the mouse barely moved between press and release, treat it as
        # a click (not a drag). Then single-click → hearts, double-click →
        # focus the parent terminal.
        dx = e.x_root - self._press_x
        dy = e.y_root - self._press_y
        if abs(dx) < 6 and abs(dy) < 6:
            # It's a click, not a drag
            now = time.time()
            if now - self._last_click_time < 0.35:
                # Double click → focus terminal
                self._last_click_time = 0.0
                self._focus_terminal()
            else:
                # Single click → hearts + little hop
                self._last_click_time = now
                self._pet()
            self._drag_hist = []
            return
        # --- Drag: apply throw physics ---
        self._drag_hist.append((time.time(), self.x, self.y))
        if len(self._drag_hist) >= 2:
            t0, x0, y0 = self._drag_hist[0]
            t1, x1, y1 = self._drag_hist[-1]
            dt = t1 - t0
            if dt > 0.01:
                # velocity in px/step (100ms units)
                self.vx = (x1 - x0) / dt * STEP_UNIT
                self.vy = (y1 - y0) / dt * STEP_UNIT
                self.vx = max(-40, min(40, self.vx))
                self.vy = max(-40, min(40, self.vy))
                sh = self.root.winfo_screenheight()
                airborne = self.y + CANVAS_H < sh - self.FLOOR_MARGIN - 2
                if abs(self.vx) + abs(self.vy) > 1.5 or airborne:
                    self.falling = True
        self._drag_hist = []

    def _pet(self):
        """Single-click: show hearts + little hop."""
        self.petted_until = time.time() + self.PET_HEARTS_MS / 1000.0
        if not self.falling and not self.dragging:
            self.vy = -6
            self.falling = True

    def _focus_terminal(self):
        """Double-click: bring the parent terminal window to the foreground.

        Uses the cached HWND found at startup (when term_pid arrived).
        """
        if sys.platform != "win32":
            return
        if not self.term_hwnd:
            sys.stderr.write(f"[pet] focus: no cached term_hwnd (term_pid={self.term_pid})\n")
            sys.stderr.flush()
            return
        try:
            _bring_window_to_front(self.term_hwnd)
            sys.stderr.write(f"[pet] focus: activated hwnd={self.term_hwnd}\n")
            sys.stderr.flush()
        except Exception as e:
            sys.stderr.write(f"[pet] focus error: {e}\n")
            sys.stderr.flush()
        except Exception as e:
            sys.stderr.write(f"[pet] focus error: {e}\n")

    # =====================================================================
    # Master loop (120 Hz physics, 60 Hz render, 12 Hz GIF advance)
    # =====================================================================

    def step(self):
        try:
            now = time.time()

            # ---- Physics: fixed 120 Hz timestep, real-dt scaled ----
            phys_dt = now - self._last_phys_time
            if phys_dt >= PHYSICS_INTERVAL:
                self._last_phys_time = now
                steps = min(phys_dt / STEP_UNIT, MAX_STEPS)
                self._expire_layers()
                self._physics_step(steps)
                self._maybe_go_home(steps)
                self.tick += 1

            # ---- Render: 60 Hz throttle ----
            if now - self._last_render_time >= RENDER_INTERVAL:
                self._last_render_time = now
                self._ensure_pet_item()
                # commit window position once per render frame
                self.root.geometry(f"+{int(self.x)}+{int(self.y)}")
                self._render_pet()
                self._render_hearts()
                self._render_bubble()

            # ---- GIF frame advance: 12 fps ----
            if now - self._last_gif_time >= GIF_INTERVAL:
                self._last_gif_time = now
                self.frame_idx += 1

            # schedule next tick ASAP; actual rate bounded by tk timer granularity
            self.root.after(1, self.step)
        except Exception as e:
            sys.stderr.write(f"[pet] step error: {e}\n")
            self.root.after(500, self.step)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hex_to_rgba(h: str):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)


# ---------------------------------------------------------------------------
# stdin loop
# ---------------------------------------------------------------------------

def stdin_loop(pet: PetWindow):
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
        except Exception as e:
            sys.stderr.write(f"[pet] stdin parse error: {e}\n")
            continue

        t = msg.get("type")
        if t == "status":
            v = msg.get("value", "idle")
            pet.root.after(0, lambda v=v: pet.set_status(v))
        elif t == "activity":
            v = msg.get("value", "idle")
            pet.root.after(0, lambda v=v: pet.set_activity(v))
        elif t == "flash":
            v = msg.get("value")
            d = int(msg.get("duration", 1500))
            pet.root.after(0, lambda: pet.flash_value(v, d))
        elif t == "alert":
            text = msg.get("text", "")
            pet.root.after(0, lambda: pet.set_alert(text))
        elif t == "clear_alert":
            pet.root.after(0, pet.clear_alert)
        elif t == "bubble":
            text = msg.get("text", "")
            d = int(msg.get("duration", 3000))
            pet.root.after(0, lambda: pet.set_bubble(text, d))
        elif t == "clear_bubble":
            pet.root.after(0, pet.clear_bubble)
        elif t == "quit":
            pet.root.after(0, pet.root.destroy)
            break
        elif t == "term_pid":
            pid = int(msg.get("pid", 0)) or None
            pet.term_pid = pid
            sys.stderr.write(f"[pet] term_pid={pid}\n")
            # Find and cache the terminal window NOW (at startup), not at
            # double-click time. The terminal is reliably findable right
            # now because it's in the foreground.
            if pid and sys.platform == "win32":
                hwnd = _find_ancestor_window(pid)
                if hwnd:
                    pet.term_hwnd = hwnd
                    title = ctypes.create_unicode_buffer(256)
                    _user32.GetWindowTextW(hwnd, title, 256)
                    sys.stderr.write(f"[pet] cached term_hwnd={hwnd} title='{title.value}'\n")
                else:
                    sys.stderr.write(f"[pet] WARNING: no terminal window found for pid={pid}\n")
            sys.stderr.flush()


def slot_watcher_loop(pet: PetWindow):
    """Background thread: poll .instances every 500ms and reposition the pet
    if its slot index changed (e.g. another pet closed).

    Uses root.after() to marshal geometry updates back to the Tk main thread.
    """
    while True:
        time.sleep(0.5)
        try:
            pet.root.after(0, pet._update_slot_position)
        except Exception:
            break  # window destroyed


def main():
    pet = PetWindow()
    t1 = threading.Thread(target=stdin_loop, args=(pet,), daemon=True)
    t1.start()
    t2 = threading.Thread(target=slot_watcher_loop, args=(pet,), daemon=True)
    t2.start()
    pet.step()
    try:
        pet.root.mainloop()
    finally:
        # Release our slot so the next pet can reuse our horizontal position.
        release_slot(pet.slot_token)


if __name__ == "__main__":
    main()
