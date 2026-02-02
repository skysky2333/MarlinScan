from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk


class MoveTabMixin:
    def _build_move_tab(self, parent: ttk.Frame) -> None:
        modes = ttk.LabelFrame(parent, text="Modes", padding=10)
        modes.pack(side=tk.TOP, fill=tk.X)

        ttk.Radiobutton(
            modes,
            text="Absolute (G90)",
            variable=self._coord_mode_var,
            value="absolute",
            command=self.apply_coord_mode,
        ).grid(row=0, column=0, sticky=tk.W, padx=(0, 10))
        ttk.Radiobutton(
            modes,
            text="Relative (G91)",
            variable=self._coord_mode_var,
            value="relative",
            command=self.apply_coord_mode,
        ).grid(row=0, column=1, sticky=tk.W)

        jog = ttk.LabelFrame(parent, text="Jog (relative)", padding=10)
        jog.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))

        ttk.Label(jog, text="Step XY (mm):").grid(row=0, column=0, sticky=tk.W)
        ttk.Combobox(
            jog,
            textvariable=self._step_xy_var,
            width=6,
            values=["0.1", "1", "5", "10", "25"],
            state="readonly",
        ).grid(row=0, column=1, sticky=tk.W, padx=(6, 0))

        ttk.Label(jog, text="Speed XY (mm/s):").grid(row=0, column=2, sticky=tk.W, padx=(16, 0))
        self.speed_xy_scale = ttk.Scale(
            jog,
            from_=1,
            to=self._max_speed_xy_var.get(),
            variable=self._speed_xy_var,
            orient=tk.HORIZONTAL,
            length=160,
            command=lambda _v: self._update_speed_labels(),
        )
        self.speed_xy_scale.grid(row=0, column=3, sticky=tk.W, padx=(6, 6))
        self.speed_xy_label = ttk.Label(jog, text="")
        self.speed_xy_label.grid(row=0, column=4, sticky=tk.W)

        ttk.Label(jog, text="Step Z (mm):").grid(row=1, column=0, sticky=tk.W, pady=(6, 0))
        ttk.Combobox(
            jog,
            textvariable=self._step_z_var,
            width=6,
            values=["0.05", "0.1", "0.5", "1", "2", "5"],
            state="readonly",
        ).grid(row=1, column=1, sticky=tk.W, padx=(6, 0), pady=(6, 0))

        ttk.Label(jog, text="Speed Z (mm/s):").grid(row=1, column=2, sticky=tk.W, padx=(16, 0), pady=(6, 0))
        self.speed_z_scale = ttk.Scale(
            jog,
            from_=1,
            to=self._max_speed_z_var.get(),
            variable=self._speed_z_var,
            orient=tk.HORIZONTAL,
            length=160,
            command=lambda _v: self._update_speed_labels(),
        )
        self.speed_z_scale.grid(row=1, column=3, sticky=tk.W, padx=(6, 6), pady=(6, 0))
        self.speed_z_label = ttk.Label(jog, text="")
        self.speed_z_label.grid(row=1, column=4, sticky=tk.W, pady=(6, 0))

        pad = ttk.Frame(jog)
        pad.grid(row=2, column=0, columnspan=5, sticky=tk.W, pady=(10, 0))
        ttk.Button(pad, text="Y+", width=6, command=lambda: self.jog_xy(0, +1)).grid(row=0, column=1)
        ttk.Button(pad, text="X-", width=6, command=lambda: self.jog_xy(-1, 0)).grid(row=1, column=0)
        ttk.Button(pad, text="X+", width=6, command=lambda: self.jog_xy(+1, 0)).grid(row=1, column=2)
        ttk.Button(pad, text="Y-", width=6, command=lambda: self.jog_xy(0, -1)).grid(row=2, column=1)

        ttk.Button(pad, text="Z+", width=6, command=lambda: self.jog_z(+1)).grid(row=0, column=3, padx=(16, 0))
        ttk.Button(pad, text="Z-", width=6, command=lambda: self.jog_z(-1)).grid(row=2, column=3, padx=(16, 0))

        # Realtime keyboard controls live in the Realtime mixin (shared logic with Bed Realtime).
        self._build_keyboard_realtime_section(parent)

        home = ttk.LabelFrame(parent, text="Homing (G28)", padding=10)
        home.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))
        ttk.Button(home, text="Home X", command=lambda: self.home("X")).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(home, text="Home Y", command=lambda: self.home("Y")).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(home, text="Home Z", command=lambda: self.home("Z")).grid(row=0, column=2, padx=(0, 6))
        ttk.Button(home, text="Home All", command=lambda: self.home(None)).grid(row=0, column=3)

        ttk.Button(home, text="Set X=0 (G92)", command=lambda: self.set_home("X")).grid(
            row=1, column=0, padx=(0, 6), pady=(8, 0)
        )
        ttk.Button(home, text="Set Y=0 (G92)", command=lambda: self.set_home("Y")).grid(
            row=1, column=1, padx=(0, 6), pady=(8, 0)
        )
        ttk.Button(home, text="Set Z=0 (G92)", command=lambda: self.set_home("Z")).grid(
            row=1, column=2, padx=(0, 6), pady=(8, 0)
        )

        go_abs = ttk.LabelFrame(parent, text="Go To (absolute)", padding=10)
        go_abs.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))

        ttk.Label(go_abs, text="X:").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(go_abs, textvariable=self._abs_x_var, width=8).grid(row=0, column=1, sticky=tk.W, padx=(4, 12))
        ttk.Label(go_abs, text="Y:").grid(row=0, column=2, sticky=tk.W)
        ttk.Entry(go_abs, textvariable=self._abs_y_var, width=8).grid(row=0, column=3, sticky=tk.W, padx=(4, 12))
        ttk.Label(go_abs, text="Z:").grid(row=0, column=4, sticky=tk.W)
        ttk.Entry(go_abs, textvariable=self._abs_z_var, width=8).grid(row=0, column=5, sticky=tk.W, padx=(4, 12))
        ttk.Button(go_abs, text="Go", command=self.go_to_absolute).grid(row=0, column=6, sticky=tk.W)

        move_rel = ttk.LabelFrame(parent, text="Move (relative)", padding=10)
        move_rel.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))

        ttk.Label(move_rel, text="dX:").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(move_rel, textvariable=self._rel_x_var, width=8).grid(row=0, column=1, sticky=tk.W, padx=(4, 12))
        ttk.Label(move_rel, text="dY:").grid(row=0, column=2, sticky=tk.W)
        ttk.Entry(move_rel, textvariable=self._rel_y_var, width=8).grid(row=0, column=3, sticky=tk.W, padx=(4, 12))
        ttk.Label(move_rel, text="dZ:").grid(row=0, column=4, sticky=tk.W)
        ttk.Entry(move_rel, textvariable=self._rel_z_var, width=8).grid(row=0, column=5, sticky=tk.W, padx=(4, 12))
        ttk.Button(move_rel, text="Move", command=self.move_relative).grid(row=0, column=6, sticky=tk.W)

        extrude = ttk.LabelFrame(parent, text="Extrude / Retract (relative E)", padding=10)
        extrude.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))

        ttk.Label(extrude, text="Amount (mm):").grid(row=0, column=0, sticky=tk.W)
        ttk.Combobox(
            extrude,
            textvariable=self._extrude_amt_var,
            width=6,
            values=["0.5", "1", "5", "10", "25"],
            state="readonly",
        ).grid(row=0, column=1, sticky=tk.W, padx=(6, 12))
        ttk.Label(extrude, text="Speed (mm/s):").grid(row=0, column=2, sticky=tk.W)
        ttk.Combobox(
            extrude,
            textvariable=self._extrude_speed_var,
            width=6,
            values=["1", "2", "3", "5", "8", "10"],
            state="readonly",
        ).grid(row=0, column=3, sticky=tk.W, padx=(6, 12))
        ttk.Button(extrude, text="Extrude", command=lambda: self.extrude(+1)).grid(row=0, column=4, padx=(0, 6))
        ttk.Button(extrude, text="Retract", command=lambda: self.extrude(-1)).grid(row=0, column=5)

        self._update_speed_labels()

    def apply_coord_mode(self) -> None:
        if self._ser is None:
            return
        mode = self._coord_mode_var.get()
        if mode == "relative":
            self._send("G91", log=False)
        else:
            self._send("G90", log=False)

    def jog_xy(self, dx_dir: int, dy_dir: int) -> None:
        step = float(self._step_xy_var.get())
        speed = float(self._speed_xy_var.get())
        dx = dx_dir * step
        dy = dy_dir * step
        feed = self._mm_s_to_mm_min(speed)
        self._jog(dx=dx, dy=dy, dz=0.0, feed=feed)

    def jog_z(self, dz_dir: int) -> None:
        step = float(self._step_z_var.get())
        speed = float(self._speed_z_var.get())
        dz = dz_dir * step
        feed = self._mm_s_to_mm_min(speed)
        self._jog(dx=0.0, dy=0.0, dz=dz, feed=feed)

    def _jog(self, *, dx: float, dy: float, dz: float, feed: int) -> None:
        if self._ser is None:
            messagebox.showerror("Jog", "Not connected.")
            return
        parts: list[str] = []
        if abs(dx) > 1e-9:
            parts.append(f"X{dx:g}")
        if abs(dy) > 1e-9:
            parts.append(f"Y{dy:g}")
        if abs(dz) > 1e-9:
            parts.append(f"Z{dz:g}")
        if not parts:
            return

        restore = self._coord_mode_var.get()
        self._send("G91", log=False)
        self._send(f"G0 {' '.join(parts)} F{feed}", log=True, timeout_s=10.0)
        if restore == "absolute":
            self._send("G90", log=False)

    def go_to_absolute(self) -> None:
        if self._ser is None:
            messagebox.showerror("Go To", "Not connected.")
            return

        try:
            x = self._float_or_none(self._abs_x_var.get())
            y = self._float_or_none(self._abs_y_var.get())
            z = self._float_or_none(self._abs_z_var.get())
        except ValueError:
            messagebox.showerror("Go To", "Invalid X/Y/Z value.")
            return

        parts: list[str] = []
        if x is not None:
            parts.append(f"X{x:g}")
        if y is not None:
            parts.append(f"Y{y:g}")
        if z is not None:
            parts.append(f"Z{z:g}")
        if not parts:
            messagebox.showinfo("Go To", "Enter at least one of X/Y/Z.")
            return

        if bool(self._confirm_motion_var.get()):
            ok = messagebox.askokcancel(
                "Go To (absolute)",
                "This will move the printer to an absolute position.\n\nContinue?",
            )
            if not ok:
                return

        speed = float(self._speed_xy_var.get())
        feed = self._mm_s_to_mm_min(speed)

        restore = self._coord_mode_var.get()
        self._send("G90", log=False)
        self._send(f"G0 {' '.join(parts)} F{feed}", log=True, timeout_s=10.0)
        if restore == "relative":
            self._send("G91", log=False)
        self._send("M114", log=False, priority="low", tag="poll_m114", timeout_s=3.0, interactive=False)
        self._poll_pending_m114 = True

    def move_relative(self) -> None:
        if self._ser is None:
            messagebox.showerror("Move", "Not connected.")
            return
        try:
            dx = self._float_or_none(self._rel_x_var.get())
            dy = self._float_or_none(self._rel_y_var.get())
            dz = self._float_or_none(self._rel_z_var.get())
        except ValueError:
            messagebox.showerror("Move", "Invalid dX/dY/dZ value.")
            return

        parts: list[str] = []
        if dx is not None:
            parts.append(f"X{dx:g}")
        if dy is not None:
            parts.append(f"Y{dy:g}")
        if dz is not None:
            parts.append(f"Z{dz:g}")
        if not parts:
            messagebox.showinfo("Move", "Enter at least one of dX/dY/dZ.")
            return

        speed = float(self._speed_xy_var.get())
        feed = self._mm_s_to_mm_min(speed)

        restore = self._coord_mode_var.get()
        self._send("G91", log=False)
        self._send(f"G0 {' '.join(parts)} F{feed}", log=True, timeout_s=10.0)
        if restore == "absolute":
            self._send("G90", log=False)
        self._send("M114", log=False, priority="low", tag="poll_m114", timeout_s=3.0, interactive=False)
        self._poll_pending_m114 = True

    def home(self, axes: str | None) -> None:
        if self._ser is None:
            messagebox.showerror("Home", "Not connected.")
            return
        if bool(self._confirm_motion_var.get()):
            label = "Home All" if not axes else f"Home {axes}"
            ok = messagebox.askokcancel(label, "This will move the printer axes.\n\nContinue?")
            if not ok:
                return
        cmd = "G28" if not axes else f"G28 {axes}"
        self._send(cmd, log=True)
        self._send("M114", log=False, priority="low", tag="poll_m114", timeout_s=3.0, interactive=False)
        self._poll_pending_m114 = True

    def set_home(self, axis: str) -> None:
        if self._ser is None:
            messagebox.showerror("Set Home", "Not connected.")
            return
        axis = axis.upper().strip()
        if axis not in {"X", "Y", "Z"}:
            return
        # Set current position as 0 on that axis (no movement).
        self._send(f"G92 {axis}0", log=True)
        self._send("M114", log=False, priority="low", tag="poll_m114", timeout_s=3.0, interactive=False)
        self._poll_pending_m114 = True

    def extrude(self, direction: int) -> None:
        if self._ser is None:
            messagebox.showerror("Extrude", "Not connected.")
            return
        try:
            amt = float(self._extrude_amt_var.get())
            speed = float(self._extrude_speed_var.get())
        except ValueError:
            messagebox.showerror("Extrude", "Invalid amount or speed.")
            return
        amt = max(0.0, amt) * (1 if direction >= 0 else -1)
        feed = self._mm_s_to_mm_min(max(0.1, speed))

        if bool(self._confirm_motion_var.get()):
            ok = messagebox.askokcancel(
                "Extrude / Retract",
                "This will move filament.\n\nMake sure the hotend is at temperature.\nContinue?",
            )
            if not ok:
                return

        self._send("M83", log=False)  # relative extrusion
        self._send(f"G1 E{amt:g} F{feed}", log=True, timeout_s=10.0)
        self._send("M400", log=False, timeout_s=60.0)
        self._send("M82", log=False)  # restore absolute extrusion (common default)

    def home_confirmed(self) -> None:
        # Backwards-compat button handler (older UI); keep it wired to new homing logic.
        self.home(None)

