from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk


class TempsTabMixin:
    def _build_temps_tab(self, parent: ttk.Frame) -> None:
        now = ttk.LabelFrame(parent, text="Current", padding=10)
        now.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(now, textvariable=self._temp_var).pack(side=tk.LEFT)
        ttk.Button(now, text="Query (M105)", command=lambda: self._send("M105")).pack(side=tk.RIGHT)

        setpoints = ttk.LabelFrame(parent, text="Setpoints", padding=10)
        setpoints.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))

        ttk.Label(setpoints, text="Hotend (M104):").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(setpoints, textvariable=self._hotend_target_var, width=6).grid(
            row=0, column=1, sticky=tk.W, padx=(6, 12)
        )
        ttk.Button(setpoints, text="Set", command=self.apply_hotend_target).grid(
            row=0, column=2, sticky=tk.W, padx=(0, 12)
        )

        ttk.Label(setpoints, text="Bed (M140):").grid(row=1, column=0, sticky=tk.W, pady=(6, 0))
        ttk.Entry(setpoints, textvariable=self._bed_target_var, width=6).grid(
            row=1, column=1, sticky=tk.W, padx=(6, 12), pady=(6, 0)
        )
        ttk.Button(setpoints, text="Set", command=self.apply_bed_target).grid(
            row=1, column=2, sticky=tk.W, padx=(0, 12), pady=(6, 0)
        )

        ttk.Button(setpoints, text="Cool Down (M104 S0 + M140 S0)", command=self.cooldown).grid(
            row=2, column=0, columnspan=3, sticky=tk.W, pady=(10, 0)
        )

        fan = ttk.LabelFrame(parent, text="Fan", padding=10)
        fan.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))

        self.fan_label = ttk.Label(fan, text="0%")
        self.fan_label.grid(row=0, column=0, sticky=tk.W)
        self.fan_scale = ttk.Scale(
            fan,
            from_=0,
            to=255,
            variable=self._fan_var,
            command=lambda _v: self._update_fan_label(),
            orient=tk.HORIZONTAL,
            length=220,
        )
        self.fan_scale.grid(row=0, column=1, sticky=tk.W, padx=(10, 10))
        ttk.Button(fan, text="Apply (M106)", command=self.apply_fan).grid(row=0, column=2, sticky=tk.W, padx=(0, 6))
        ttk.Button(fan, text="Off (M107)", command=self.fan_off).grid(row=0, column=3, sticky=tk.W)
        fan.grid_columnconfigure(1, weight=1)

        self._update_fan_label()

    def _update_fan_label(self) -> None:
        try:
            value = int(round(float(self._fan_var.get())))
        except Exception:
            value = 0
        value = max(0, min(255, value))
        pct = int(round(value * 100 / 255))
        self.fan_label.configure(text=f"{pct}%")

    def set_hotend_temp(self) -> None:
        if self._ser is None:
            messagebox.showerror("Hotend", "Not connected.")
            return
        temp = simpledialog.askinteger("Hotend Temp", "Set hotend temp (°C):", minvalue=0, maxvalue=300)
        if temp is None:
            return
        self._hotend_target_var.set(str(temp))
        self.apply_hotend_target()

    def set_bed_temp(self) -> None:
        if self._ser is None:
            messagebox.showerror("Bed", "Not connected.")
            return
        temp = simpledialog.askinteger("Bed Temp", "Set bed temp (°C):", minvalue=0, maxvalue=120)
        if temp is None:
            return
        self._bed_target_var.set(str(temp))
        self.apply_bed_target()

    def apply_hotend_target(self) -> None:
        if self._ser is None:
            messagebox.showerror("Hotend", "Not connected.")
            return
        try:
            temp = int(float(self._hotend_target_var.get().strip()))
        except ValueError:
            messagebox.showerror("Hotend", "Invalid hotend temperature.")
            return
        temp = max(0, min(300, temp))
        self._send(f"M104 S{temp}", log=True)

    def apply_bed_target(self) -> None:
        if self._ser is None:
            messagebox.showerror("Bed", "Not connected.")
            return
        try:
            temp = int(float(self._bed_target_var.get().strip()))
        except ValueError:
            messagebox.showerror("Bed", "Invalid bed temperature.")
            return
        temp = max(0, min(120, temp))
        self._send(f"M140 S{temp}", log=True)

    def cooldown(self) -> None:
        self._hotend_target_var.set("0")
        self._bed_target_var.set("0")
        if self._ser is None:
            return
        self._send_many([("M104 S0", True), ("M140 S0", True)])

    def apply_fan(self) -> None:
        if self._ser is None:
            messagebox.showerror("Fan", "Not connected.")
            return
        self._update_fan_label()
        value = int(round(float(self._fan_var.get())))
        value = max(0, min(255, value))
        if value <= 0:
            self.fan_off()
            return
        self._send(f"M106 S{value}", log=True)

    def fan_off(self) -> None:
        self._fan_var.set(0.0)
        self._update_fan_label()
        if self._ser is None:
            return
        self._send("M107", log=True)

