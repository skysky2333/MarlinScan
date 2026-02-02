from __future__ import annotations

import math
import time
import tkinter as tk
from tkinter import messagebox, ttk

from ..models import GCodeJob
from ..parsers import parse_m114


class RealtimeMixin:
    def _build_bed_realtime_tab(self, parent: ttk.Frame) -> None:
        intro = ttk.LabelFrame(parent, text="Mouse → Realtime XY Jog", padding=10)
        intro.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(
            intro,
            text=(
                "Streams tiny XY moves to follow your mouse. This is best-effort (Marlin queues moves).\n"
                "Tip: effective speed is min(Speed XY, step × Hz). Try 40 Hz, 1.0 mm step, Speed XY 100–250."
            ),
            justify=tk.LEFT,
        ).pack(side=tk.TOP, anchor=tk.W)

        body = ttk.Frame(parent)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(10, 0))

        left = ttk.Frame(body)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        right = ttk.LabelFrame(body, text="Controls", padding=10)
        right.pack(side=tk.RIGHT, fill=tk.Y, padx=(10, 0))

        self.rt_canvas = tk.Canvas(left, width=360, height=360, background="white", highlightthickness=1)
        self.rt_canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.rt_canvas.bind("<Configure>", lambda _e: self._redraw_rt_bed())
        self.rt_canvas.bind("<Motion>", self._on_rt_motion)
        self.rt_canvas.bind("<Leave>", self._on_rt_leave)
        self.rt_canvas.bind("<ButtonPress-1>", self._on_rt_press)
        self.rt_canvas.bind("<ButtonRelease-1>", self._on_rt_release)

        btns = ttk.Frame(right)
        btns.pack(side=tk.TOP, fill=tk.X)
        self.rt_start_btn = ttk.Button(btns, text="Start", command=self._rt_start)
        self.rt_start_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.rt_stop_btn = ttk.Button(btns, text="Stop", command=self._rt_stop, state="disabled")
        self.rt_stop_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))

        ttk.Label(right, textvariable=self._rt_status_var).pack(side=tk.TOP, anchor=tk.W, pady=(10, 0))

        ttk.Checkbutton(right, text="Hold left mouse to move", variable=self._rt_hold_mouse_var).pack(
            side=tk.TOP, anchor=tk.W, pady=(10, 0)
        )
        ttk.Checkbutton(right, text="Home X/Y on Start (G28 X Y)", variable=self._rt_home_xy_var).pack(
            side=tk.TOP, anchor=tk.W, pady=(6, 0)
        )
        ttk.Checkbutton(right, text="Sync each tick (M400)", variable=self._rt_sync_m400_var).pack(
            side=tk.TOP, anchor=tk.W, pady=(6, 0)
        )

        grid = ttk.Frame(right)
        grid.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))
        ttk.Label(grid, text="Tick (Hz):").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(grid, textvariable=self._rt_tick_hz_var, width=8).grid(row=0, column=1, sticky=tk.W, padx=(6, 0))
        ttk.Label(grid, text="Max step/tick (mm):").grid(row=1, column=0, sticky=tk.W, pady=(6, 0))
        ttk.Entry(grid, textvariable=self._rt_step_mm_var, width=8).grid(
            row=1, column=1, sticky=tk.W, padx=(6, 0), pady=(6, 0)
        )
        ttk.Label(grid, text="Deadband (mm):").grid(row=2, column=0, sticky=tk.W, pady=(6, 0))
        ttk.Entry(grid, textvariable=self._rt_deadband_mm_var, width=8).grid(
            row=2, column=1, sticky=tk.W, padx=(6, 0), pady=(6, 0)
        )
        ttk.Label(grid, text="Buffer (ms):").grid(row=3, column=0, sticky=tk.W, pady=(6, 0))
        ttk.Entry(grid, textvariable=self._rt_buffer_ms_var, width=8).grid(
            row=3, column=1, sticky=tk.W, padx=(6, 0), pady=(6, 0)
        )
        grid.grid_columnconfigure(1, weight=1)

        boost = ttk.LabelFrame(right, text="Motion Boost (optional)", padding=10)
        boost.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))
        ttk.Checkbutton(boost, text="Apply M201/M204/M205 while running", variable=self._rt_boost_motion_var).pack(
            side=tk.TOP, anchor=tk.W
        )
        boost_grid = ttk.Frame(boost)
        boost_grid.pack(side=tk.TOP, fill=tk.X, pady=(6, 0))
        ttk.Label(boost_grid, text="M201 XY (mm/s^2):").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(boost_grid, textvariable=self._rt_boost_m201_xy_var, width=8).grid(
            row=0, column=1, sticky=tk.W, padx=(6, 0)
        )
        ttk.Label(boost_grid, text="M204 (mm/s^2):").grid(row=1, column=0, sticky=tk.W, pady=(6, 0))
        ttk.Entry(boost_grid, textvariable=self._rt_boost_m204_var, width=8).grid(
            row=1, column=1, sticky=tk.W, padx=(6, 0), pady=(6, 0)
        )
        ttk.Label(boost_grid, text="M205 J:").grid(row=2, column=0, sticky=tk.W, pady=(6, 0))
        ttk.Entry(boost_grid, textvariable=self._rt_boost_junction_var, width=8).grid(
            row=2, column=1, sticky=tk.W, padx=(6, 0), pady=(6, 0)
        )
        boost_grid.grid_columnconfigure(1, weight=1)

        ttk.Button(right, text="Target = Current", command=self._rt_target_from_current).pack(
            side=tk.TOP, fill=tk.X, pady=(10, 0)
        )

        self._redraw_rt_bed()

    def _build_keyboard_realtime_section(self, parent: ttk.Frame) -> None:
        kb = ttk.LabelFrame(parent, text="Realtime Keyboard (experimental)", padding=10)
        kb.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))
        ttk.Label(
            kb,
            text="Controls: Arrow keys = X/Y, Shift = Z+, Control = Z-. Hold keys to move. Combos work (e.g. Up+Right).",
            justify=tk.LEFT,
        ).grid(row=0, column=0, columnspan=6, sticky=tk.W)

        kb_btns = ttk.Frame(kb)
        kb_btns.grid(row=1, column=0, sticky=tk.W, pady=(8, 0))
        self.kb_start_btn = ttk.Button(kb_btns, text="Start", command=self._kb_start)
        self.kb_start_btn.grid(row=0, column=0, sticky=tk.W)
        self.kb_stop_btn = ttk.Button(kb_btns, text="Stop", command=self._kb_stop, state="disabled")
        self.kb_stop_btn.grid(row=0, column=1, sticky=tk.W, padx=(8, 0))
        ttk.Label(kb, textvariable=self._kb_status_var).grid(
            row=1, column=1, columnspan=5, sticky=tk.W, padx=(10, 0), pady=(8, 0)
        )

        kb_grid = ttk.Frame(kb)
        kb_grid.grid(row=2, column=0, columnspan=6, sticky=tk.W, pady=(10, 0))
        ttk.Label(kb_grid, text="Tick (Hz):").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(kb_grid, textvariable=self._kb_tick_hz_var, width=8).grid(
            row=0, column=1, sticky=tk.W, padx=(6, 12)
        )
        ttk.Label(kb_grid, text="Max step XY/tick (mm):").grid(row=0, column=2, sticky=tk.W)
        ttk.Entry(kb_grid, textvariable=self._kb_step_xy_mm_var, width=8).grid(
            row=0, column=3, sticky=tk.W, padx=(6, 12)
        )
        ttk.Label(kb_grid, text="Max step Z/tick (mm):").grid(row=0, column=4, sticky=tk.W)
        ttk.Entry(kb_grid, textvariable=self._kb_step_z_mm_var, width=8).grid(row=0, column=5, sticky=tk.W, padx=(6, 0))

        ttk.Label(kb_grid, text="Buffer (ms):").grid(row=1, column=0, sticky=tk.W, pady=(6, 0))
        ttk.Entry(kb_grid, textvariable=self._kb_buffer_ms_var, width=8).grid(
            row=1, column=1, sticky=tk.W, padx=(6, 12), pady=(6, 0)
        )
        ttk.Checkbutton(kb_grid, text="Sync each tick (M400)", variable=self._kb_sync_m400_var).grid(
            row=1, column=2, columnspan=2, sticky=tk.W, pady=(6, 0)
        )
        ttk.Checkbutton(kb_grid, text="Motion Boost (M201/M204/M205 J)", variable=self._rt_boost_motion_var).grid(
            row=1, column=4, columnspan=2, sticky=tk.W, pady=(6, 0)
        )

    def _rt_target_from_current(self) -> None:
        if self._current_x is None or self._current_y is None:
            return
        x_min, x_max, y_min, y_max, _z_min, _z_max = self._bed_bounds()
        self._rt_target_x_var.set(self._clamp(float(self._current_x), x_min, x_max))
        self._rt_target_y_var.set(self._clamp(float(self._current_y), y_min, y_max))
        if self._rt_virtual_x is None or self._rt_virtual_y is None:
            self._rt_virtual_x = float(self._current_x)
            self._rt_virtual_y = float(self._current_y)
        self._rt_request_redraw(force=True)

    def _rt_canvas_square(self) -> tuple[float, float, float, int, int]:
        if not hasattr(self, "rt_canvas"):
            return (0.0, 0.0, 1.0, 1, 1)
        w = max(1, int(self.rt_canvas.winfo_width()))
        h = max(1, int(self.rt_canvas.winfo_height()))
        size = float(min(w, h))
        ox = (w - size) / 2.0
        oy = (h - size) / 2.0
        return (ox, oy, size, w, h)

    def _rt_canvas_to_bed(self, px: float, py: float) -> tuple[float, float]:
        if not hasattr(self, "rt_canvas"):
            return (0.0, 0.0)
        ox, oy, size, _w, _h = self._rt_canvas_square()
        x_min, x_max, y_min, y_max, _z_min, _z_max = self._bed_bounds()
        px = self._clamp(px, ox, ox + size)
        py = self._clamp(py, oy, oy + size)
        x_n = (px - ox) / size
        y_n = 1.0 - ((py - oy) / size)
        x = x_min + x_n * (x_max - x_min)
        y = y_min + y_n * (y_max - y_min)
        return (x, y)

    def _rt_bed_to_canvas(self, x: float, y: float) -> tuple[float, float]:
        if not hasattr(self, "rt_canvas"):
            return (0.0, 0.0)
        ox, oy, size, _w, _h = self._rt_canvas_square()
        x_min, x_max, y_min, y_max, _z_min, _z_max = self._bed_bounds()
        x_n = (x - x_min) / (x_max - x_min)
        y_n = (y - y_min) / (y_max - y_min)
        px = ox + (x_n * size)
        py = oy + ((1.0 - y_n) * size)
        return (px, py)

    def _rt_request_redraw(self, *, force: bool = False) -> None:
        if not hasattr(self, "rt_canvas"):
            return
        if getattr(self, "_rt_redraw_after_id", None) is not None:
            return

        min_interval_s = 0.0
        if self._rt_active:
            min_interval_s = 1.0 / 30.0

        delay_ms = 0
        if not force and min_interval_s > 0.0:
            last = getattr(self, "_rt_last_redraw_time", None)
            if last is not None:
                dt = time.monotonic() - float(last)
                if dt < min_interval_s:
                    delay_ms = int(round((min_interval_s - dt) * 1000.0))

        self._rt_redraw_after_id = self.after(delay_ms, self._rt_do_redraw)

    def _rt_do_redraw(self) -> None:
        self._rt_redraw_after_id = None
        self._rt_last_redraw_time = time.monotonic()
        self._redraw_rt_bed()

    def _redraw_rt_bed(self) -> None:
        if not hasattr(self, "rt_canvas"):
            return
        x_min, x_max, y_min, y_max, _z_min, _z_max = self._bed_bounds()

        c = self.rt_canvas
        c.delete("all")
        ox, oy, size, _w, _h = self._rt_canvas_square()

        # Border (square inside the canvas to keep aspect ratio)
        c.create_rectangle(ox + 2, oy + 2, ox + size - 2, oy + size - 2, outline="#222", width=2)

        # Grid every 20mm (approx).
        step = 20.0
        x = x_min
        while x <= x_max + 1e-6:
            px, _ = self._rt_bed_to_canvas(x, y_min)
            c.create_line(px, oy, px, oy + size, fill="#eee")
            x += step
        y = y_min
        while y <= y_max + 1e-6:
            _, py = self._rt_bed_to_canvas(x_min, y)
            c.create_line(ox, py, ox + size, py, fill="#eee")
            y += step

        # Target marker (mouse)
        tx = self._clamp(float(self._rt_target_x_var.get()), x_min, x_max)
        ty = self._clamp(float(self._rt_target_y_var.get()), y_min, y_max)
        self._rt_target_x_var.set(tx)
        self._rt_target_y_var.set(ty)
        tpx, tpy = self._rt_bed_to_canvas(tx, ty)
        c.create_line(tpx - 8, tpy, tpx + 8, tpy, fill="#d11", width=2)
        c.create_line(tpx, tpy - 8, tpx, tpy + 8, fill="#d11", width=2)

        # Current position marker (from M114)
        if self._current_x is not None and self._current_y is not None:
            cx = self._clamp(float(self._current_x), x_min, x_max)
            cy = self._clamp(float(self._current_y), y_min, y_max)
            cpx, cpy = self._rt_bed_to_canvas(cx, cy)
            r = 5
            c.create_oval(cpx - r, cpy - r, cpx + r, cpy + r, fill="#1a5", outline="#0b3")

        # Virtual position marker (what we've commanded so far)
        if self._rt_virtual_x is not None and self._rt_virtual_y is not None:
            vx = self._clamp(float(self._rt_virtual_x), x_min, x_max)
            vy = self._clamp(float(self._rt_virtual_y), y_min, y_max)
            vpx, vpy = self._rt_bed_to_canvas(vx, vy)
            r = 4
            c.create_oval(vpx - r, vpy - r, vpx + r, vpy + r, fill="#19f", outline="#06c")

        # Legend
        legend = "green=current, red=mouse target, blue=commanded"
        if self._rt_active:
            if bool(self._rt_hold_mouse_var.get()):
                legend += " (hold LMB)"
            legend += " [RUNNING]"
        c.create_text(ox + 8, oy + size - 10, anchor=tk.W, text=legend, fill="#555")

    def _on_rt_motion(self, event: tk.Event) -> None:
        x, y = self._rt_canvas_to_bed(float(event.x), float(event.y))
        x_min, x_max, y_min, y_max, _z_min, _z_max = self._bed_bounds()
        x = self._clamp(x, x_min, x_max)
        y = self._clamp(y, y_min, y_max)
        self._rt_target_x_var.set(x)
        self._rt_target_y_var.set(y)
        self._rt_mouse_inside = True
        self._rt_request_redraw()

    def _on_rt_leave(self, _event: tk.Event) -> None:
        self._rt_mouse_inside = False
        self._rt_mouse_down = False
        self._rt_request_redraw()

    def _on_rt_press(self, event: tk.Event) -> None:
        self._rt_mouse_down = True
        self._on_rt_motion(event)

    def _on_rt_release(self, _event: tk.Event) -> None:
        self._rt_mouse_down = False
        self._rt_request_redraw()

    @staticmethod
    def _rt_float(text: str, *, default: float) -> float:
        try:
            return float(str(text).strip())
        except Exception:
            return float(default)

    def _rt_apply_motion_boost(self) -> None:
        if self._ser is None:
            return
        if self._worker is None:
            return
        if not bool(self._rt_boost_motion_var.get()):
            return
        if self._rt_boost_applied:
            return

        self._rt_boost_saved_m201_xy = (
            self._config.max_accel_mm_s2.get("X"),
            self._config.max_accel_mm_s2.get("Y"),
        )
        self._rt_boost_saved_m204 = self._config.accel_print_mm_s2
        self._rt_boost_saved_junction = self._config.junction_deviation

        m201_xy = int(round(self._rt_float(self._rt_boost_m201_xy_var.get(), default=3000.0)))
        m201_xy = max(100, min(20000, m201_xy))
        m204 = int(round(self._rt_float(self._rt_boost_m204_var.get(), default=3000.0)))
        m204 = max(100, min(20000, m204))
        junc = float(self._rt_float(self._rt_boost_junction_var.get(), default=0.2))
        junc = max(0.0, min(2.0, junc))

        # Apply only X/Y for M201 (do not touch Z settings).
        self._send(f"M201 X{m201_xy} Y{m201_xy}", log=True, priority="high", tag="rt_boost_m201", timeout_s=3.0)
        self._send(
            f"M204 P{m204} R{m204} T{m204}",
            log=True,
            priority="high",
            tag="rt_boost_m204",
            timeout_s=3.0,
        )
        self._send(f"M205 J{junc:g}", log=True, priority="high", tag="rt_boost_m205", timeout_s=3.0)
        self._rt_boost_applied = True

    def _rt_restore_motion_boost(self) -> None:
        if self._ser is None:
            return
        if self._worker is None:
            return
        if not self._rt_boost_applied:
            return

        x0y0 = self._rt_boost_saved_m201_xy
        m204 = self._rt_boost_saved_m204
        junc = self._rt_boost_saved_junction

        if x0y0 is not None and (x0y0[0] is not None) and (x0y0[1] is not None):
            self._send(
                f"M201 X{float(x0y0[0]):g} Y{float(x0y0[1]):g}",
                log=True,
                priority="low",
                tag="rt_restore_m201",
                timeout_s=3.0,
                interactive=False,
            )
        if m204 is not None:
            v = int(round(float(m204)))
            self._send(
                f"M204 P{v} R{v} T{v}",
                log=True,
                priority="low",
                tag="rt_restore_m204",
                timeout_s=3.0,
                interactive=False,
            )
        if junc is not None:
            self._send(
                f"M205 J{float(junc):g}",
                log=True,
                priority="low",
                tag="rt_restore_m205",
                timeout_s=3.0,
                interactive=False,
            )

        self._rt_boost_applied = False
        self._rt_boost_saved_m201_xy = None
        self._rt_boost_saved_m204 = None
        self._rt_boost_saved_junction = None

    def _rt_start(self) -> None:
        if self._ser is None:
            messagebox.showerror("Bed Realtime", "Not connected.")
            return
        if self._kb_active:
            messagebox.showerror("Bed Realtime", "Stop Realtime Keyboard first.")
            return

        if bool(self._confirm_motion_var.get()):
            ok = messagebox.askokcancel(
                "Bed Realtime",
                "This will stream many tiny XY moves based on your mouse.\n\n"
                "Make sure:\n"
                "  • The nozzle is at a safe Z height\n"
                "  • The bed is clear\n"
                "  • You can hit EMERGENCY STOP if needed\n\n"
                "Continue?",
            )
            if not ok:
                return
            if bool(self._rt_home_xy_var.get()):
                ok2 = messagebox.askokcancel(
                    "Bed Realtime: Home X/Y",
                    "Home X/Y on Start is enabled (G28 X Y).\n\n"
                    "Note: some firmwares may raise Z slightly during homing, even when only X/Y are requested.\n\n"
                    "Continue?",
                )
                if not ok2:
                    return

        if self._rt_active:
            return

        self._rt_active = True
        if hasattr(self, "rt_start_btn"):
            self.rt_start_btn.configure(state="disabled")
        if hasattr(self, "rt_stop_btn"):
            self.rt_stop_btn.configure(state="normal")
        self._rt_mouse_down = False
        self._rt_mouse_inside = False
        self._rt_pending_acks = 0
        self._rt_pending_start = False
        self._rt_queue_time_s = 0.0
        self._rt_last_tick_time = time.monotonic()

        self._rt_restore_coord_mode = self._coord_mode_var.get()
        self._coord_mode_var.set("relative")
        self._send("G91", log=False, priority="high", tag="rt_g91", timeout_s=3.0, interactive=False)

        if bool(self._rt_home_xy_var.get()):
            self._rt_virtual_x = None
            self._rt_virtual_y = None
            self._rt_pending_start = True
            self._rt_target_x_var.set(0.0)
            self._rt_target_y_var.set(0.0)
            self._send("G28 X Y", log=True, priority="high", tag="rt_g28_xy", timeout_s=120.0, interactive=False)
            self._send("M400", log=False, priority="high", tag="rt_m400_home", timeout_s=300.0, interactive=False)
            self._send("G91", log=False, priority="high", tag="rt_g91_post_home", timeout_s=3.0, interactive=False)
            self._send("M114", log=False, priority="high", tag="rt_m114_start", timeout_s=3.0, interactive=False)
            self._rt_status_var.set("Starting (homing X/Y)…")
        elif self._current_x is None or self._current_y is None:
            self._rt_virtual_x = None
            self._rt_virtual_y = None
            self._rt_pending_start = True
            self._send("M114", log=False, priority="high", tag="rt_m114_start", timeout_s=3.0, interactive=False)
            self._rt_status_var.set("Starting (waiting for M114)…")
        else:
            self._rt_virtual_x = float(self._current_x)
            self._rt_virtual_y = float(self._current_y)
            self._rt_target_from_current()
            self._rt_status_var.set("Running")

        self._rt_apply_motion_boost()
        self.after(0, self._rt_tick)
        self._rt_request_redraw(force=True)

    def _rt_stop(self) -> None:
        if not self._rt_active and not self._rt_pending_start:
            return
        self._rt_active = False
        if hasattr(self, "rt_start_btn"):
            self.rt_start_btn.configure(state="normal")
        if hasattr(self, "rt_stop_btn"):
            self.rt_stop_btn.configure(state="disabled")
        self._rt_pending_start = False
        self._rt_mouse_down = False
        self._rt_mouse_inside = False
        self._rt_pending_acks = 0
        self._rt_status_var.set("Stopped")
        self._rt_queue_time_s = 0.0
        self._rt_last_tick_time = None

        self._rt_restore_motion_boost()

        restore = self._rt_restore_coord_mode
        self._rt_restore_coord_mode = None
        if restore in {"absolute", "relative"}:
            self._coord_mode_var.set(restore)
            self.apply_coord_mode()

        self._rt_request_redraw(force=True)

    def _rt_tick(self) -> None:
        if not self._rt_active:
            return

        hz = self._rt_float(self._rt_tick_hz_var.get(), default=40.0)
        hz = max(1.0, min(100.0, hz))
        interval_ms = int(round(1000.0 / hz))

        step_cap_mm = self._rt_float(self._rt_step_mm_var.get(), default=1.0)
        step_cap_mm = max(0.01, min(20.0, step_cap_mm))

        deadband = self._rt_float(self._rt_deadband_mm_var.get(), default=0.2)
        deadband = max(0.0, min(10.0, deadband))

        buffer_ms = self._rt_float(self._rt_buffer_ms_var.get(), default=60.0)
        buffer_ms = max(0.0, min(500.0, buffer_ms))
        buffer_s = buffer_ms / 1000.0

        now = time.monotonic()
        if self._rt_last_tick_time is None:
            self._rt_last_tick_time = now
        elapsed = max(0.0, now - self._rt_last_tick_time)
        self._rt_last_tick_time = now
        self._rt_queue_time_s = max(0.0, float(self._rt_queue_time_s) - elapsed)

        sync_each_tick = bool(self._rt_sync_m400_var.get())

        should_move = True
        if bool(self._rt_hold_mouse_var.get()) and (not self._rt_mouse_down):
            should_move = False
        if not self._rt_mouse_inside:
            should_move = False

        if self._ser is None or self._worker is None:
            self._rt_status_var.set("Stopped (disconnected)")
            self._rt_active = False
            return

        # Avoid piling up commands if the printer stops responding.
        target_moves = max(1, int(math.ceil(max(buffer_s, 1.0 / hz) * hz)))
        max_pending = max(6, target_moves + 8)
        if self._rt_pending_acks > max_pending:
            self._rt_status_var.set(
                f"Running (backlog {self._rt_pending_acks}, q≈{self._rt_queue_time_s*1000:.0f}ms)"
            )
            self.after(interval_ms, self._rt_tick)
            return

        if (not should_move) or self._rt_virtual_x is None or self._rt_virtual_y is None:
            if self._rt_virtual_x is None or self._rt_virtual_y is None:
                prefix = "Starting" if self._rt_pending_start else "Running"
                self._rt_status_var.set(f"{prefix} (waiting for position, q≈{self._rt_queue_time_s*1000:.0f}ms)…")
            else:
                self._rt_status_var.set(f"Running (idle, q≈{self._rt_queue_time_s*1000:.0f}ms)")
            self.after(interval_ms, self._rt_tick)
            return

        # For smooth motion, keep a small amount of motion queued (buffer_s).
        # Too much buffer increases input lag; too little can cause planner starvation (choppiness).
        desired_queue_s = max(buffer_s, 1.0 / hz)
        if sync_each_tick:
            desired_queue_s = 1.0 / hz

        x_min, x_max, y_min, y_max, _z_min, _z_max = self._bed_bounds()
        v_max = max(1e-6, float(self._speed_xy_var.get()))
        max_per_tick = min(step_cap_mm, v_max / hz)

        segments_sent = 0
        last_speed = 0.0
        last_dist = 0.0

        while self._rt_queue_time_s + 1e-9 < desired_queue_s:
            vx = float(self._rt_virtual_x)
            vy = float(self._rt_virtual_y)

            tx = self._clamp(float(self._rt_target_x_var.get()), x_min, x_max)
            ty = self._clamp(float(self._rt_target_y_var.get()), y_min, y_max)
            self._rt_target_x_var.set(tx)
            self._rt_target_y_var.set(ty)

            dx = tx - vx
            dy = ty - vy
            dist = math.hypot(dx, dy)
            last_dist = dist
            if dist <= deadband:
                break

            move_len = min(dist, max_per_tick)
            if move_len <= 1e-9:
                break

            if dist > move_len:
                scale = move_len / dist
                dx *= scale
                dy *= scale

            nx = vx + dx
            ny = vy + dy
            actual_len = math.hypot(dx, dy)
            if actual_len <= 1e-9:
                break

            speed = max(1e-6, min(v_max, actual_len * hz))
            feed = max(1, self._mm_s_to_mm_min(speed))
            sent = self._send(
                f"G0 X{dx:g} Y{dy:g} F{feed}",
                log=False,
                priority="high",
                tag="rt_move",
                timeout_s=10.0,
                interactive=False,
            )
            if not sent:
                break

            self._rt_pending_acks += 1
            last_speed = speed
            self._rt_virtual_x, self._rt_virtual_y = nx, ny
            self._rt_queue_time_s += actual_len / speed
            segments_sent += 1

            if sync_each_tick:
                if self._send("M400", log=False, priority="high", tag="rt_m400", timeout_s=300.0, interactive=False):
                    self._rt_pending_acks += 1
                break

            if segments_sent >= 20:
                break

        if segments_sent > 0:
            q_ms = self._rt_queue_time_s * 1000.0
            self._rt_status_var.set(f"Running (v≈{last_speed:.0f} mm/s, q≈{q_ms:.0f}ms)")
        elif last_dist <= deadband:
            self._rt_status_var.set(f"Running (on target, q≈{self._rt_queue_time_s*1000:.0f}ms)")
        else:
            self._rt_status_var.set(f"Running (idle, q≈{self._rt_queue_time_s*1000:.0f}ms)")

        self._rt_request_redraw()
        self.after(interval_ms, self._rt_tick)

    @staticmethod
    def _clamp_delta(pos: float, delta: float, lo: float, hi: float) -> float:
        # If we're already outside a bound, prevent moving farther out.
        if delta > 0:
            if pos >= hi:
                return 0.0
            if (pos + delta) > hi:
                return hi - pos
            return delta
        if delta < 0:
            if pos <= lo:
                return 0.0
            if (pos + delta) < lo:
                return lo - pos
            return delta
        return 0.0

    def _kb_key_of_event(self, event: tk.Event) -> str | None:
        key = str(getattr(event, "keysym", "") or "")
        if key in {"Up", "Down", "Left", "Right"}:
            return key
        if key in {"Shift_L", "Shift_R"}:
            return "Shift"
        if key in {"Control_L", "Control_R"}:
            return "Control"
        return None

    def _kb_on_key_press(self, event: tk.Event) -> str | None:
        if not self._kb_active:
            return None
        key = self._kb_key_of_event(event)
        if key is None:
            return None
        self._kb_keys_down.add(key)
        return "break"

    def _kb_on_key_release(self, event: tk.Event) -> str | None:
        if not self._kb_active:
            return None
        key = self._kb_key_of_event(event)
        if key is None:
            return None
        self._kb_keys_down.discard(key)
        return "break"

    def _kb_install_bindings(self) -> None:
        if getattr(self, "_kb_bind_press_id", None) is not None:
            return
        try:
            self._kb_bind_press_id = self.bind("<KeyPress>", self._kb_on_key_press, add="+")
            self._kb_bind_release_id = self.bind("<KeyRelease>", self._kb_on_key_release, add="+")
        except Exception:
            self._kb_bind_press_id = None
            self._kb_bind_release_id = None

    def _kb_remove_bindings(self) -> None:
        press_id = getattr(self, "_kb_bind_press_id", None)
        release_id = getattr(self, "_kb_bind_release_id", None)
        try:
            if press_id:
                self.unbind("<KeyPress>", press_id)
            if release_id:
                self.unbind("<KeyRelease>", release_id)
        except Exception:
            pass
        self._kb_bind_press_id = None
        self._kb_bind_release_id = None

    def _kb_start(self) -> None:
        if self._ser is None:
            messagebox.showerror("Realtime Keyboard", "Not connected.")
            return
        if self._rt_active:
            messagebox.showerror("Realtime Keyboard", "Stop Bed Realtime first.")
            return
        if self._kb_active:
            return

        if bool(self._confirm_motion_var.get()):
            ok = messagebox.askokcancel(
                "Realtime Keyboard",
                "This will continuously jog while keys are held.\n\n"
                "Controls:\n"
                "  • Arrow keys = X/Y\n"
                "  • Shift = Z+\n"
                "  • Control = Z-\n\n"
                "Make sure the nozzle is at a safe Z height and the bed is clear.\n\nContinue?",
            )
            if not ok:
                return

        self._kb_active = True
        self._kb_pending_start = True
        self._kb_pending_acks = 0
        self._kb_keys_down.clear()
        self._kb_queue_time_s = 0.0
        self._kb_last_tick_time = time.monotonic()
        self._kb_virtual_x = None
        self._kb_virtual_y = None
        self._kb_virtual_z = None

        if hasattr(self, "kb_start_btn"):
            self.kb_start_btn.configure(state="disabled")
        if hasattr(self, "kb_stop_btn"):
            self.kb_stop_btn.configure(state="normal")

        # Prefer keeping focus on the main window so arrow keys don't get consumed by Entry widgets.
        try:
            self.focus_set()
        except Exception:
            pass

        self._kb_restore_coord_mode = self._coord_mode_var.get()
        self._coord_mode_var.set("relative")
        self._send("G91", log=False, priority="high", tag="kb_g91", timeout_s=3.0, interactive=False)

        # Ensure we have an up-to-date position snapshot for bounds.
        self._send("M114", log=False, priority="high", tag="kb_m114_start", timeout_s=3.0, interactive=False)
        self._kb_status_var.set("Starting (waiting for M114)…")

        # Optional motion boost (shared settings with Bed Realtime).
        self._rt_apply_motion_boost()

        # Install key listeners (bind on the root so we don't clobber other future bind_all handlers).
        self._kb_install_bindings()

        self.after(0, self._kb_tick)

    def _kb_stop(self) -> None:
        if not self._kb_active and not self._kb_pending_start:
            return
        self._kb_active = False
        self._kb_pending_start = False
        self._kb_pending_acks = 0
        self._kb_keys_down.clear()
        self._kb_queue_time_s = 0.0
        self._kb_last_tick_time = None
        self._kb_status_var.set("Stopped")

        self._kb_remove_bindings()

        self._rt_restore_motion_boost()

        restore = self._kb_restore_coord_mode
        self._kb_restore_coord_mode = None
        if restore in {"absolute", "relative"}:
            self._coord_mode_var.set(restore)
            self.apply_coord_mode()

        if hasattr(self, "kb_start_btn"):
            self.kb_start_btn.configure(state="normal")
        if hasattr(self, "kb_stop_btn"):
            self.kb_stop_btn.configure(state="disabled")

    def _kb_tick(self) -> None:
        if not self._kb_active:
            return

        hz = self._rt_float(self._kb_tick_hz_var.get(), default=60.0)
        hz = max(1.0, min(100.0, hz))
        interval_ms = int(round(1000.0 / hz))
        dt = 1.0 / hz

        step_xy_cap = self._rt_float(self._kb_step_xy_mm_var.get(), default=4.0)
        step_xy_cap = max(0.01, min(50.0, step_xy_cap))
        step_z_cap = self._rt_float(self._kb_step_z_mm_var.get(), default=0.5)
        step_z_cap = max(0.001, min(50.0, step_z_cap))

        buffer_ms = self._rt_float(self._kb_buffer_ms_var.get(), default=30.0)
        buffer_ms = max(0.0, min(500.0, buffer_ms))
        buffer_s = buffer_ms / 1000.0

        now = time.monotonic()
        if self._kb_last_tick_time is None:
            self._kb_last_tick_time = now
        elapsed = max(0.0, now - self._kb_last_tick_time)
        self._kb_last_tick_time = now
        self._kb_queue_time_s = max(0.0, float(self._kb_queue_time_s) - elapsed)

        if self._ser is None or self._worker is None:
            self._kb_status_var.set("Stopped (disconnected)")
            self._kb_active = False
            return

        # Keep focus on the main window so arrow keys are reliably captured.
        if self._kb_keys_down:
            try:
                self.focus_set()
            except Exception:
                pass

        # Backpressure: don't queue unbounded commands if the printer stops responding.
        target_moves = max(1, int(math.ceil(max(buffer_s, dt) * hz)))
        max_pending = max(6, target_moves + 8)
        if self._kb_pending_acks > max_pending:
            self._kb_status_var.set(
                f"Running (backlog {self._kb_pending_acks}, q≈{self._kb_queue_time_s*1000:.0f}ms)"
            )
            self.after(interval_ms, self._kb_tick)
            return

        # Need an initial position to enforce bounds.
        if self._kb_pending_start or self._kb_virtual_x is None or self._kb_virtual_y is None or self._kb_virtual_z is None:
            self._kb_status_var.set(f"Starting (waiting for position, q≈{self._kb_queue_time_s*1000:.0f}ms)…")
            self.after(interval_ms, self._kb_tick)
            return

        dx_dir = (1 if "Right" in self._kb_keys_down else 0) + (-1 if "Left" in self._kb_keys_down else 0)
        dy_dir = (1 if "Up" in self._kb_keys_down else 0) + (-1 if "Down" in self._kb_keys_down else 0)
        dz_dir = (1 if "Shift" in self._kb_keys_down else 0) + (-1 if "Control" in self._kb_keys_down else 0)

        if dx_dir == 0 and dy_dir == 0 and dz_dir == 0:
            self._kb_status_var.set(f"Running (idle, q≈{self._kb_queue_time_s*1000:.0f}ms)")
            self.after(interval_ms, self._kb_tick)
            return

        sync_each_tick = bool(self._kb_sync_m400_var.get())
        desired_queue_s = max(buffer_s, dt)
        if sync_each_tick:
            desired_queue_s = dt

        x_min, x_max, y_min, y_max, z_min, z_max = self._bed_bounds()

        v_xy = max(1e-6, float(self._speed_xy_var.get()))
        v_z = max(1e-6, float(self._speed_z_var.get()))

        segments_sent = 0
        last_speed = 0.0

        while self._kb_queue_time_s + 1e-9 < desired_queue_s:
            vx = float(self._kb_virtual_x)
            vy = float(self._kb_virtual_y)
            vz = float(self._kb_virtual_z)

            dx = 0.0
            dy = 0.0
            if dx_dir != 0 or dy_dir != 0:
                norm = math.hypot(dx_dir, dy_dir)
                if norm > 1e-9:
                    dx = (dx_dir / norm) * v_xy * dt
                    dy = (dy_dir / norm) * v_xy * dt
                    xy_len = math.hypot(dx, dy)
                    if xy_len > step_xy_cap:
                        s = step_xy_cap / max(1e-9, xy_len)
                        dx *= s
                        dy *= s

            dz = 0.0
            if dz_dir != 0:
                dz = float(dz_dir) * v_z * dt
                if abs(dz) > step_z_cap:
                    dz = math.copysign(step_z_cap, dz)

            dx = self._clamp_delta(vx, dx, x_min, x_max)
            dy = self._clamp_delta(vy, dy, y_min, y_max)
            dz = self._clamp_delta(vz, dz, z_min, z_max)

            if abs(dx) <= 1e-9 and abs(dy) <= 1e-9 and abs(dz) <= 1e-9:
                break

            parts: list[str] = []
            if abs(dx) > 1e-9:
                parts.append(f"X{dx:g}")
            if abs(dy) > 1e-9:
                parts.append(f"Y{dy:g}")
            if abs(dz) > 1e-9:
                parts.append(f"Z{dz:g}")
            if not parts:
                break

            actual_len = math.sqrt((dx * dx) + (dy * dy) + (dz * dz))
            speed = max(1e-6, min(actual_len / dt, 500.0))
            feed = max(1, self._mm_s_to_mm_min(speed))

            sent = self._send(
                f"G0 {' '.join(parts)} F{feed}",
                log=False,
                priority="high",
                tag="kb_move",
                timeout_s=10.0,
                interactive=False,
            )
            if not sent:
                break

            self._kb_pending_acks += 1
            self._kb_virtual_x = vx + dx
            self._kb_virtual_y = vy + dy
            self._kb_virtual_z = vz + dz
            self._kb_queue_time_s += dt
            last_speed = speed
            segments_sent += 1

            if sync_each_tick:
                if self._send("M400", log=False, priority="high", tag="kb_m400", timeout_s=300.0, interactive=False):
                    self._kb_pending_acks += 1
                break

            if segments_sent >= 20:
                break

        if segments_sent > 0:
            self._kb_status_var.set(f"Running (v≈{last_speed:.0f} mm/s, q≈{self._kb_queue_time_s*1000:.0f}ms)")
        else:
            self._kb_status_var.set(f"Running (q≈{self._kb_queue_time_s*1000:.0f}ms)")

        self.after(interval_ms, self._kb_tick)

    def _realtime_handle_job_done(self, job: GCodeJob, lines: list[str], ok: bool) -> None:
        if job.tag in {"rt_move", "rt_m400"} and self._rt_pending_acks > 0:
            self._rt_pending_acks -= 1
        if job.tag in {"kb_move", "kb_m400"} and self._kb_pending_acks > 0:
            self._kb_pending_acks -= 1

        if job.tag == "rt_m114_start":
            if not ok:
                self._rt_pending_start = False
                self._rt_active = False
                self._rt_status_var.set("Stopped (M114 failed)")
            elif self._rt_active and self._rt_pending_start:
                for line in lines:
                    if line.lstrip().lower().startswith("count"):
                        continue
                    pos = parse_m114(line)
                    if pos is None:
                        continue
                    x, y, _z, _e = pos
                    if x is None or y is None:
                        continue
                    self._rt_virtual_x = float(x)
                    self._rt_virtual_y = float(y)
                    self._rt_target_from_current()
                    self._rt_pending_start = False
                    self._rt_status_var.set("Running")
                    self._rt_request_redraw(force=True)
                    break

        if job.tag == "kb_m114_start":
            if not ok:
                self._kb_pending_start = False
                self._kb_active = False
                self._kb_status_var.set("Stopped (M114 failed)")
                if hasattr(self, "kb_start_btn"):
                    self.kb_start_btn.configure(state="normal")
                if hasattr(self, "kb_stop_btn"):
                    self.kb_stop_btn.configure(state="disabled")
            elif self._kb_active and self._kb_pending_start:
                for line in lines:
                    if line.lstrip().lower().startswith("count"):
                        continue
                    pos = parse_m114(line)
                    if pos is None:
                        continue
                    x, y, z, _e = pos
                    if x is None or y is None or z is None:
                        continue
                    self._kb_virtual_x = float(x)
                    self._kb_virtual_y = float(y)
                    self._kb_virtual_z = float(z)
                    self._kb_pending_start = False
                    self._kb_status_var.set("Running")
                    break

    def _realtime_cleanup_on_disconnect(self) -> None:
        self._kb_active = False
        self._kb_pending_start = False
        self._kb_pending_acks = 0
        self._kb_keys_down.clear()
        self._kb_status_var.set("Stopped (disconnected)")
        self._kb_restore_coord_mode = None
        self._kb_virtual_x = None
        self._kb_virtual_y = None
        self._kb_virtual_z = None
        self._kb_queue_time_s = 0.0
        self._kb_last_tick_time = None
        self._kb_remove_bindings()
        if hasattr(self, "kb_start_btn"):
            self.kb_start_btn.configure(state="normal")
        if hasattr(self, "kb_stop_btn"):
            self.kb_stop_btn.configure(state="disabled")

        self._rt_active = False
        self._rt_pending_start = False
        self._rt_pending_acks = 0
        self._rt_mouse_down = False
        self._rt_mouse_inside = False
        self._rt_status_var.set("Stopped (disconnected)")
        self._rt_queue_time_s = 0.0
        self._rt_last_tick_time = None
        self._rt_virtual_x = None
        self._rt_virtual_y = None
        self._rt_restore_motion_boost()
        if hasattr(self, "rt_start_btn"):
            self.rt_start_btn.configure(state="normal")
        if hasattr(self, "rt_stop_btn"):
            self.rt_stop_btn.configure(state="disabled")
        self._rt_request_redraw(force=True)

