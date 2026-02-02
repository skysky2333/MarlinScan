from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk


class BedTabMixin:
    def _build_bed_tab(self, parent: ttk.Frame) -> None:
        settings = ttk.LabelFrame(parent, text="Work Area (mm)", padding=10)
        settings.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(settings, text="X min:").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(settings, textvariable=self._bed_x_min_var, width=8, state="readonly").grid(
            row=0, column=1, sticky=tk.W, padx=(6, 12)
        )
        ttk.Label(settings, text="X max:").grid(row=0, column=2, sticky=tk.W)
        ttk.Entry(settings, textvariable=self._bed_x_max_var, width=8).grid(row=0, column=3, sticky=tk.W, padx=(6, 12))

        ttk.Label(settings, text="Y min:").grid(row=0, column=4, sticky=tk.W)
        ttk.Entry(settings, textvariable=self._bed_y_min_var, width=8, state="readonly").grid(
            row=0, column=5, sticky=tk.W, padx=(6, 12)
        )
        ttk.Label(settings, text="Y max:").grid(row=0, column=6, sticky=tk.W)
        ttk.Entry(settings, textvariable=self._bed_y_max_var, width=8).grid(row=0, column=7, sticky=tk.W, padx=(6, 0))

        ttk.Label(settings, text="Z max:").grid(row=1, column=0, sticky=tk.W, pady=(6, 0))
        ttk.Entry(settings, textvariable=self._bed_z_max_var, width=8).grid(
            row=1, column=1, sticky=tk.W, padx=(6, 12), pady=(6, 0)
        )
        ttk.Button(settings, text="Redraw", command=self._redraw_bed).grid(
            row=1, column=2, sticky=tk.W, pady=(6, 0)
        )

        motion = ttk.LabelFrame(parent, text="Absolute Positioning", padding=10)
        motion.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(10, 0))

        left = ttk.Frame(motion)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        right = ttk.Frame(motion)
        right.pack(side=tk.RIGHT, fill=tk.Y)

        self.bed_canvas = tk.Canvas(left, width=360, height=360, background="white", highlightthickness=1)
        self.bed_canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.bed_canvas.bind("<Button-1>", self._on_bed_click)
        self.bed_canvas.bind("<Configure>", lambda _e: self._redraw_bed())

        ttk.Label(right, text="Z target (mm):").pack(side=tk.TOP, anchor=tk.W)
        self.z_scale = ttk.Scale(
            right,
            from_=0.0,
            to=max(1.0, float(self._bed_z_max_var.get()) - float(self._bed_z_min_var.get())),
            variable=self._z_raw_var,
            orient=tk.VERTICAL,
            length=320,
            command=self._on_z_raw_changed,
        )
        self.z_scale.pack(side=tk.TOP, fill=tk.Y, pady=(6, 6))
        self.z_scale.bind("<ButtonRelease-1>", self._on_z_scale_released)
        self.z_target_label = ttk.Label(right, text="")
        self.z_target_label.pack(side=tk.TOP, anchor=tk.W)

        buttons = ttk.Frame(parent)
        buttons.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))

        self._bed_status_var = tk.StringVar(value="Current: X:? Y:? Z:?   Target: X:? Y:? Z:?")
        ttk.Label(buttons, textvariable=self._bed_status_var).pack(side=tk.LEFT)

        ttk.Checkbutton(buttons, text="Move on click", variable=self._bed_click_move_var).pack(
            side=tk.RIGHT, padx=(6, 0)
        )
        ttk.Button(buttons, text="Target = Current", command=self._target_from_current).pack(
            side=tk.RIGHT, padx=(6, 0)
        )
        ttk.Button(buttons, text="Move To Target", command=self.move_to_target_confirmed).pack(
            side=tk.RIGHT, padx=(6, 0)
        )

        self._update_speed_labels()
        self._sync_z_slider_from_target()
        self._redraw_bed()

    def _bed_bounds(self) -> tuple[float, float, float, float, float, float]:
        # Treat the UI bed/work area as 0..Xmax and 0..Ymax.
        # Some firmwares report negative X/Y after homing (off-bed). The UI should not allow targeting those.
        x_min = 0.0
        x_max = float(self._bed_x_max_var.get())
        y_min = 0.0
        y_max = float(self._bed_y_max_var.get())
        z_min = float(self._bed_z_min_var.get())
        z_max = float(self._bed_z_max_var.get())
        if float(self._bed_x_min_var.get()) != 0.0:
            self._bed_x_min_var.set(0.0)
        if float(self._bed_y_min_var.get()) != 0.0:
            self._bed_y_min_var.set(0.0)
        if x_max <= x_min:
            x_max = x_min + 1.0
        if y_max <= y_min:
            y_max = y_min + 1.0
        if z_max <= z_min:
            z_max = z_min + 1.0
        return (x_min, x_max, y_min, y_max, z_min, z_max)

    @staticmethod
    def _clamp(value: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, value))

    def _bed_canvas_square(self) -> tuple[float, float, float, int, int]:
        w = max(1, int(self.bed_canvas.winfo_width()))
        h = max(1, int(self.bed_canvas.winfo_height()))
        size = float(min(w, h))
        ox = (w - size) / 2.0
        oy = (h - size) / 2.0
        return (ox, oy, size, w, h)

    def _canvas_to_bed(self, px: float, py: float) -> tuple[float, float]:
        if not hasattr(self, "bed_canvas"):
            return (0.0, 0.0)
        ox, oy, size, _w, _h = self._bed_canvas_square()
        x_min, x_max, y_min, y_max, _z_min, _z_max = self._bed_bounds()
        px = self._clamp(px, ox, ox + size)
        py = self._clamp(py, oy, oy + size)
        x_n = (px - ox) / size
        y_n = 1.0 - ((py - oy) / size)
        x = x_min + x_n * (x_max - x_min)
        y = y_min + y_n * (y_max - y_min)
        return (x, y)

    def _bed_to_canvas(self, x: float, y: float) -> tuple[float, float]:
        if not hasattr(self, "bed_canvas"):
            return (0.0, 0.0)
        ox, oy, size, _w, _h = self._bed_canvas_square()
        x_min, x_max, y_min, y_max, _z_min, _z_max = self._bed_bounds()
        x_n = (x - x_min) / (x_max - x_min)
        y_n = (y - y_min) / (y_max - y_min)
        px = ox + (x_n * size)
        py = oy + ((1.0 - y_n) * size)
        return (px, py)

    def _update_bed_status(self) -> None:
        if not hasattr(self, "_bed_status_var"):
            return
        tx = float(self._target_x_var.get())
        ty = float(self._target_y_var.get())
        tz = float(self._target_z_var.get())
        cx = self._current_x
        cy = self._current_y
        cz = self._current_z

        cur = (
            f"X:{cx:.2f} Y:{cy:.2f} Z:{cz:.2f}"
            if (cx is not None and cy is not None and cz is not None)
            else "X:? Y:? Z:?"
        )
        self._bed_status_var.set(f"Current: {cur}   Target: X:{tx:.2f} Y:{ty:.2f} Z:{tz:.2f}")
        if hasattr(self, "z_target_label"):
            self.z_target_label.configure(text=f"{tz:.2f}")

    def _sync_z_slider_from_target(self) -> None:
        # ttk.Scale doesn't reliably support reversed ranges on some platforms; use an inverted raw value.
        _x_min, _x_max, _y_min, _y_max, z_min, z_max = self._bed_bounds()
        target = self._clamp(float(self._target_z_var.get()), z_min, z_max)
        self._target_z_var.set(target)
        rng = max(1.0, z_max - z_min)
        raw = self._clamp(z_max - target, 0.0, rng)
        self._z_raw_var.set(raw)

    def _on_z_raw_changed(self, _value: str | None = None) -> None:
        _x_min, _x_max, _y_min, _y_max, z_min, z_max = self._bed_bounds()
        rng = max(1.0, z_max - z_min)
        raw = self._clamp(float(self._z_raw_var.get()), 0.0, rng)
        target = self._clamp(z_max - raw, z_min, z_max)
        self._target_z_var.set(target)
        self._update_bed_status()

    def _on_z_scale_released(self, _event: tk.Event) -> None:
        # If "Move on click" is enabled, treat Z slider interaction as a target selection too.
        if self._ser is not None and bool(self._bed_click_move_var.get()):
            self.move_to_target_confirmed()

    def _redraw_bed(self) -> None:
        if not hasattr(self, "bed_canvas"):
            return
        x_min, x_max, y_min, y_max, z_min, z_max = self._bed_bounds()
        if hasattr(self, "z_scale"):
            rng = max(1.0, z_max - z_min)
            self.z_scale.configure(from_=0.0, to=rng)
            self._sync_z_slider_from_target()

        self._update_bed_status()

        c = self.bed_canvas
        c.delete("all")
        ox, oy, size, _w, _h = self._bed_canvas_square()

        # Border (square inside the canvas to keep aspect ratio)
        c.create_rectangle(ox + 2, oy + 2, ox + size - 2, oy + size - 2, outline="#222", width=2)

        # Grid every 20mm (approx).
        step = 20.0
        x = x_min
        while x <= x_max + 1e-6:
            px, _ = self._bed_to_canvas(x, y_min)
            c.create_line(px, oy, px, oy + size, fill="#eee")
            x += step
        y = y_min
        while y <= y_max + 1e-6:
            _, py = self._bed_to_canvas(x_min, y)
            c.create_line(ox, py, ox + size, py, fill="#eee")
            y += step

        # Target marker
        tx = self._clamp(float(self._target_x_var.get()), x_min, x_max)
        ty = self._clamp(float(self._target_y_var.get()), y_min, y_max)
        self._target_x_var.set(tx)
        self._target_y_var.set(ty)
        tpx, tpy = self._bed_to_canvas(tx, ty)
        c.create_line(tpx - 8, tpy, tpx + 8, tpy, fill="#d11", width=2)
        c.create_line(tpx, tpy - 8, tpx, tpy + 8, fill="#d11", width=2)

        # Current position marker
        if self._current_x is not None and self._current_y is not None:
            cx = self._clamp(float(self._current_x), x_min, x_max)
            cy = self._clamp(float(self._current_y), y_min, y_max)
            cpx, cpy = self._bed_to_canvas(cx, cy)
            r = 5
            c.create_oval(cpx - r, cpy - r, cpx + r, cpy + r, fill="#1a5", outline="#0b3")

        # Legend
        c.create_text(ox + 8, oy + size - 10, anchor=tk.W, text="green=current, red=target", fill="#555")

    def _on_bed_click(self, event: tk.Event) -> None:
        x_min, x_max, y_min, y_max, _z_min, _z_max = self._bed_bounds()
        x, y = self._canvas_to_bed(float(event.x), float(event.y))
        x = self._clamp(x, x_min, x_max)
        y = self._clamp(y, y_min, y_max)
        self._target_x_var.set(x)
        self._target_y_var.set(y)
        self._redraw_bed()
        if self._ser is not None and bool(self._bed_click_move_var.get()):
            self.move_to_target_confirmed()

    def _target_from_current(self) -> None:
        if self._current_x is None or self._current_y is None or self._current_z is None:
            return
        self._target_x_var.set(float(self._current_x))
        self._target_y_var.set(float(self._current_y))
        self._target_z_var.set(float(self._current_z))
        self._redraw_bed()

    def move_to_target_confirmed(self) -> None:
        if self._ser is None:
            messagebox.showerror("Move To Target", "Not connected.")
            return

        x_min, x_max, y_min, y_max, z_min, z_max = self._bed_bounds()
        x = self._clamp(float(self._target_x_var.get()), x_min, x_max)
        y = self._clamp(float(self._target_y_var.get()), y_min, y_max)
        z = self._clamp(float(self._target_z_var.get()), z_min, z_max)

        if bool(self._confirm_motion_var.get()):
            ok = messagebox.askokcancel(
                "Move To Target",
                f"Move to X:{x:.2f} Y:{y:.2f} Z:{z:.2f} ?\n\nThis will move the printer.",
            )
            if not ok:
                return

        speed = float(self._speed_xy_var.get())
        feed = self._mm_s_to_mm_min(speed)

        restore = self._coord_mode_var.get()
        self._send("G90", log=False)
        self._send(f"G0 X{x:.3f} Y{y:.3f} Z{z:.3f} F{feed}", log=True, timeout_s=10.0)
        if restore == "relative":
            self._send("G91", log=False)
        self._send("M114", log=False, priority="low", tag="poll_m114", timeout_s=3.0, interactive=False)
        self._poll_pending_m114 = True

