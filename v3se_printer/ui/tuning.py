from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk


class TuningTabMixin:
    def _build_tuning_tab(self, parent: ttk.Frame) -> None:
        speed = ttk.LabelFrame(parent, text="Speed Override (M220)", padding=10)
        speed.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(speed, text="Feedrate %:").grid(row=0, column=0, sticky=tk.W)
        ttk.Spinbox(
            speed,
            from_=10,
            to=300,
            textvariable=self._feed_override_var,
            width=6,
        ).grid(row=0, column=1, sticky=tk.W, padx=(6, 12))
        ttk.Button(speed, text="Apply", command=self.apply_feed_override).grid(row=0, column=2, sticky=tk.W)

        flow = ttk.LabelFrame(parent, text="Flow Override (M221)", padding=10)
        flow.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))

        ttk.Label(flow, text="Flow %:").grid(row=0, column=0, sticky=tk.W)
        ttk.Spinbox(
            flow,
            from_=10,
            to=300,
            textvariable=self._flow_override_var,
            width=6,
        ).grid(row=0, column=1, sticky=tk.W, padx=(6, 12))
        ttk.Button(flow, text="Apply", command=self.apply_flow_override).grid(row=0, column=2, sticky=tk.W)

        accel = ttk.LabelFrame(parent, text="Acceleration (M204)", padding=10)
        accel.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))

        ttk.Label(accel, text="Accel (mm/s^2):").grid(row=0, column=0, sticky=tk.W)
        self.accel_label = ttk.Label(accel, text="")
        self.accel_label.grid(row=0, column=1, sticky=tk.W, padx=(6, 12))
        self.accel_scale = ttk.Scale(
            accel,
            from_=100,
            to=5000,
            variable=self._accel_var,
            orient=tk.HORIZONTAL,
            length=260,
            command=lambda _v: self._update_accel_label(),
        )
        self.accel_scale.grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=(8, 0))
        ttk.Button(accel, text="Apply (P/R/T)", command=self.apply_acceleration).grid(
            row=0, column=2, rowspan=2, sticky=tk.W
        )
        self._update_accel_label()

    def _update_accel_label(self) -> None:
        if not hasattr(self, "accel_label"):
            return
        try:
            value = int(round(float(self._accel_var.get())))
        except Exception:
            value = 2500
        value = max(100, min(5000, value))
        self.accel_label.configure(text=f"{value}")

    def _apply_speed_limits(self) -> None:
        max_xy = max(1.0, float(self._max_speed_xy_var.get()))
        max_z = max(1.0, float(self._max_speed_z_var.get()))
        if self._speed_xy_var.get() > max_xy:
            self._speed_xy_var.set(max_xy)
        if self._speed_z_var.get() > max_z:
            self._speed_z_var.set(max_z)
        if hasattr(self, "speed_xy_scale"):
            self.speed_xy_scale.configure(to=max_xy)
        if hasattr(self, "speed_z_scale"):
            self.speed_z_scale.configure(to=max_z)
        self._update_speed_labels()

    def _update_speed_labels(self) -> None:
        if hasattr(self, "speed_xy_label"):
            self.speed_xy_label.configure(text=f"{float(self._speed_xy_var.get()):.0f}")
        if hasattr(self, "speed_z_label"):
            self.speed_z_label.configure(text=f"{float(self._speed_z_var.get()):.1f}")

    def apply_feed_override(self) -> None:
        if self._ser is None:
            messagebox.showerror("Speed Override", "Not connected.")
            return
        value = int(self._feed_override_var.get())
        value = max(10, min(300, value))
        self._send(f"M220 S{value}", log=True)

    def apply_flow_override(self) -> None:
        if self._ser is None:
            messagebox.showerror("Flow Override", "Not connected.")
            return
        value = int(self._flow_override_var.get())
        value = max(10, min(300, value))
        self._send(f"M221 S{value}", log=True)

    def apply_acceleration(self) -> None:
        if self._ser is None:
            messagebox.showerror("Acceleration", "Not connected.")
            return
        value = int(round(float(self._accel_var.get())))
        value = max(100, min(5000, value))
        self._accel_var.set(float(value))
        self._update_accel_label()
        # Set print/travel/retract to the same value for now.
        self._send(f"M204 P{value} R{value} T{value}", log=True)

