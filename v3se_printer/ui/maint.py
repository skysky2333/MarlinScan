from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from ..models import GCodeJob


class MaintTabMixin:
    def _build_maint_tab(self, parent: ttk.Frame) -> None:
        leveling = ttk.LabelFrame(parent, text="Leveling", padding=10)
        leveling.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(leveling, text="Auto Level (G29)", command=self.auto_level_confirmed).grid(
            row=0, column=0, sticky=tk.W, padx=(0, 6), pady=(0, 6)
        )
        ttk.Button(leveling, text="Mesh On (M420 S1)", command=lambda: self._send("M420 S1")).grid(
            row=0, column=1, sticky=tk.W, padx=(0, 6), pady=(0, 6)
        )
        ttk.Button(leveling, text="Mesh Off (M420 S0)", command=lambda: self._send("M420 S0")).grid(
            row=0, column=2, sticky=tk.W, pady=(0, 6)
        )

        eeprom = ttk.LabelFrame(parent, text="EEPROM", padding=10)
        eeprom.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))
        ttk.Button(eeprom, text="Save (M500)", command=lambda: self._send("M500")).grid(
            row=0, column=0, sticky=tk.W, padx=(0, 6), pady=(0, 6)
        )
        ttk.Button(eeprom, text="Load (M501)", command=lambda: self._send("M501")).grid(
            row=0, column=1, sticky=tk.W, padx=(0, 6), pady=(0, 6)
        )
        ttk.Button(eeprom, text="Defaults (M502)", command=self.reset_defaults_confirmed).grid(
            row=0, column=2, sticky=tk.W, pady=(0, 6)
        )
        ttk.Button(eeprom, text="Report (M503)", command=lambda: self._send("M503")).grid(
            row=1, column=0, sticky=tk.W
        )

    def auto_level_confirmed(self) -> None:
        if self._ser is None:
            messagebox.showerror("Auto Level", "Not connected.")
            return
        ok = messagebox.askokcancel(
            "Auto Level (G29)",
            "This will probe the bed (G29).\n\nMake sure the bed is clear. Continue?",
        )
        if ok:
            self._send("G29", log=True)

    def reset_defaults_confirmed(self) -> None:
        if self._ser is None:
            messagebox.showerror("Defaults", "Not connected.")
            return
        ok = messagebox.askokcancel(
            "Restore Defaults (M502)",
            "This restores EEPROM defaults (M502).\n\nYou may want to Save (M500) afterward.\nContinue?",
        )
        if ok:
            self._send("M502", log=True)

    def _maybe_show_home_prompt(self) -> None:
        if not self._home_prompt_pending:
            return
        if self._home_prompt_shown:
            return
        if not (self._home_prompt_seen_m105 and self._home_prompt_seen_m114):
            return
        if self._ser is None:
            return

        self._home_prompt_shown = True
        self.after(0, self._show_home_prompt_dialog)

    def _startup_home_update_controls(self) -> None:
        if self._startup_home_dialog is None:
            return

        busy_jobs = self._startup_home_pending_jobs > 0
        busy_kb = bool(getattr(self, "_kb_active", False)) or bool(getattr(self, "_kb_pending_start", False))
        busy = busy_jobs or busy_kb
        for btn in self._startup_home_buttons:
            try:
                btn.configure(state=("disabled" if busy else "normal"))
            except Exception:
                continue

        done = all(self._startup_home_axis_status.get(a) in {"auto", "manual"} for a in ("X", "Y", "Z"))
        if self._startup_home_continue_btn is not None:
            self._startup_home_continue_btn.configure(state=("normal" if (done and (not busy)) else "disabled"))
        if done and (not busy):
            self._startup_home_status_var.set("All axes are set. Click Continue.")

        start_btn = getattr(self, "_startup_kb_start_btn", None)
        stop_btn = getattr(self, "_startup_kb_stop_btn", None)
        if start_btn is not None:
            start_btn.configure(state=("disabled" if busy else "normal"))
        if stop_btn is not None:
            stop_btn.configure(state=("normal" if busy_kb else "disabled"))

    def _startup_home_continue(self) -> None:
        if self._startup_home_dialog is None:
            return
        if self._startup_home_pending_jobs > 0:
            return
        done = all(self._startup_home_axis_status.get(a) in {"auto", "manual"} for a in ("X", "Y", "Z"))
        if not done:
            messagebox.showinfo(
                "Startup Homing",
                "Please complete setup for X, Y, and Z before continuing.",
                parent=self._startup_home_dialog,
            )
            return

        if bool(getattr(self, "_kb_active", False)) or bool(getattr(self, "_kb_pending_start", False)):
            self._kb_stop()

        self._home_prompt_pending = False
        if self._deferred_m503 and self._ser is not None:
            # Safe to do now; do not block the startup homing actions.
            self._send("M503", log=True, tag="m503", priority="low", timeout_s=30.0, interactive=False)
        self._deferred_m503 = False
        try:
            self._startup_home_dialog.destroy()
        finally:
            self._startup_home_dialog = None
            self._startup_home_continue_btn = None
            self._startup_home_buttons = []
            self._startup_kb_start_btn = None
            self._startup_kb_stop_btn = None

    def _startup_home_handle_job_done(self, job: GCodeJob, ok: bool) -> None:
        if self._startup_home_dialog is None:
            return
        tag = job.tag.strip()
        if tag.startswith("startup_motors:"):
            # startup_motors:on|off
            parts = tag.split(":")
            if len(parts) == 2:
                action = parts[1].strip().lower()
            else:
                action = ""

            if ok:
                if action == "on":
                    self._startup_home_motors_enabled = True
                    self._startup_home_motors_var.set("Motors: Enabled")
                elif action == "off":
                    self._startup_home_motors_enabled = False
                    self._startup_home_motors_var.set("Motors: Disabled")
                else:
                    self._startup_home_motors_var.set("Motors: ?")
                # Don't overwrite a more specific in-progress message.
                if self._startup_home_pending_jobs <= 1:
                    self._startup_home_status_var.set("OK. Continue with axis setup.")
            else:
                self._startup_home_motors_var.set("Motors: ?")
                self._startup_home_status_var.set("Motor command failed. See the log and retry.")
                try:
                    messagebox.showerror(
                        "Motor Command Failed",
                        f"The command did not return ok:\n\n{job.command}\n\nSee the log and retry.",
                        parent=self._startup_home_dialog,
                    )
                except Exception:
                    pass

            self._startup_home_pending_jobs = max(0, int(self._startup_home_pending_jobs) - 1)
            self._startup_home_update_controls()
            return

        if not tag.startswith("startup_home:"):
            return

        # startup_home:auto|manual:X|Y|Z|ALL
        parts = tag.split(":")
        if len(parts) != 3:
            self._startup_home_pending_jobs = max(0, int(self._startup_home_pending_jobs) - 1)
            self._startup_home_update_controls()
            return
        _prefix, mode, axis = parts
        mode = mode.lower().strip()
        axis = axis.upper().strip()
        if mode not in {"auto", "manual"}:
            self._startup_home_pending_jobs = max(0, int(self._startup_home_pending_jobs) - 1)
            self._startup_home_update_controls()
            return

        axes = ("X", "Y", "Z") if axis == "ALL" else (axis,)
        for a in axes:
            if a not in {"X", "Y", "Z"}:
                self._startup_home_pending_jobs = max(0, int(self._startup_home_pending_jobs) - 1)
                self._startup_home_update_controls()
                return

        if ok:
            for a in axes:
                self._startup_home_axis_status[a] = mode
                self._startup_home_axis_vars[a].set("Auto OK" if mode == "auto" else "Manual OK")
            self._startup_home_status_var.set("OK. Complete the remaining axes to continue.")

            # Refresh position display without blocking motion/queue.
            if not self._poll_pending_m114:
                self._send("M114", log=False, priority="low", tag="poll_m114", timeout_s=3.0, interactive=False)
                self._poll_pending_m114 = True
        else:
            for a in axes:
                self._startup_home_axis_vars[a].set("Error")
            self._startup_home_status_var.set("Command failed. See the log and retry.")
            try:
                messagebox.showerror(
                    "Startup Homing Failed",
                    f"The command did not return ok:\n\n{job.command}\n\nSee the log and retry.",
                    parent=self._startup_home_dialog,
                )
            except Exception:
                pass

        self._startup_home_pending_jobs = max(0, int(self._startup_home_pending_jobs) - 1)
        self._startup_home_update_controls()

    def _show_home_prompt_dialog(self) -> None:
        if self._ser is None or self._worker is None:
            return
        if self._startup_home_dialog is not None:
            try:
                self._startup_home_dialog.lift()
                self._startup_home_dialog.focus_force()
            except Exception:
                pass
            return

        self._startup_home_axis_status = {"X": "pending", "Y": "pending", "Z": "pending"}
        for axis in ("X", "Y", "Z"):
            self._startup_home_axis_vars[axis].set("Pending")
        self._startup_home_pending_jobs = 0
        self._startup_home_motors_enabled = None
        self._startup_home_motors_var.set("Motors: ?")
        self._startup_home_status_var.set("Choose Auto/Manual for each axis. Continue unlocks once X/Y/Z are OK.")

        dlg = tk.Toplevel(self)
        self._startup_home_dialog = dlg
        dlg.title("Startup Homing / Coordinate Setup")
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(False, False)

        def disconnect() -> None:
            if bool(getattr(self, "_kb_active", False)) or bool(getattr(self, "_kb_pending_start", False)):
                self._kb_stop()
            try:
                dlg.destroy()
            finally:
                self.disconnect()

        dlg.protocol("WM_DELETE_WINDOW", disconnect)

        body = ttk.Frame(dlg, padding=12)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        ttk.Label(
            body,
            text=(
                "Set up coordinates after connecting.\n\n"
                "Auto = home using endstops/probe (G28) — WILL MOVE.\n"
                "Manual = set current position as 0 (G92) — no motion.\n"
                "Note: Auto Z usually requires Auto X + Auto Y first."
            ),
            justify=tk.LEFT,
        ).grid(row=0, column=0, columnspan=4, sticky=tk.W)

        ttk.Label(body, textvariable=self._startup_home_status_var, foreground="#333").grid(
            row=1, column=0, columnspan=4, sticky=tk.W, pady=(10, 0)
        )

        motors = ttk.Frame(body)
        motors.grid(row=2, column=0, columnspan=4, sticky=tk.W, pady=(10, 0))
        ttk.Label(motors, textvariable=self._startup_home_motors_var, width=16).pack(side=tk.LEFT)

        kb = ttk.LabelFrame(body, text="Manual Positioning (Keyboard Jog)", padding=10)
        kb.grid(row=3, column=0, columnspan=4, sticky=tk.W + tk.E, pady=(10, 0))
        ttk.Label(
            kb,
            text=(
                "Use this to move before choosing Manual X/Y/Z=0.\n"
                "Controls: Arrow keys = X/Y, Shift = Z+, Control = Z-."
            ),
            justify=tk.LEFT,
        ).grid(row=0, column=0, columnspan=4, sticky=tk.W)

        kb_btns = ttk.Frame(kb)
        kb_btns.grid(row=1, column=0, sticky=tk.W, pady=(8, 0))

        def start_kb() -> None:
            self._kb_start(bind_widget=dlg, enforce_bounds=False, speed_xy_cap=30.0, speed_z_cap=2.0)
            self._startup_home_update_controls()

        def stop_kb() -> None:
            self._kb_stop()
            self._startup_home_update_controls()

        self._startup_kb_start_btn = ttk.Button(kb_btns, text="Start Jog", command=start_kb)
        self._startup_kb_start_btn.grid(row=0, column=0, sticky=tk.W)
        self._startup_kb_stop_btn = ttk.Button(kb_btns, text="Stop Jog", command=stop_kb, state="disabled")
        self._startup_kb_stop_btn.grid(row=0, column=1, sticky=tk.W, padx=(8, 0))
        ttk.Label(kb, textvariable=self._kb_status_var).grid(
            row=1, column=1, columnspan=3, sticky=tk.W, padx=(10, 0), pady=(8, 0)
        )
        kb.grid_columnconfigure(0, weight=1)

        hdr = ttk.Frame(body)
        hdr.grid(row=4, column=0, columnspan=4, sticky=tk.W, pady=(10, 0))
        ttk.Label(hdr, text="Axis", width=6).grid(row=0, column=0, sticky=tk.W)
        ttk.Label(hdr, text="Status", width=16).grid(row=0, column=1, sticky=tk.W, padx=(10, 0))
        ttk.Label(hdr, text="Actions").grid(row=0, column=2, sticky=tk.W, padx=(10, 0))

        self._startup_home_buttons = []

        def send_startup_motors(enable: bool) -> None:
            if self._worker is None or self._ser is None:
                return
            if self._startup_home_pending_jobs > 0:
                return
            if bool(getattr(self, "_kb_active", False)) or bool(getattr(self, "_kb_pending_start", False)):
                self._kb_stop()

            cmd = "M17" if enable else "M84"
            tag = "startup_motors:on" if enable else "startup_motors:off"
            self._startup_home_pending_jobs = 1
            self._startup_home_motors_var.set("Motors: Enabling…" if enable else "Motors: Disabling…")
            self._startup_home_status_var.set(f"Running: {cmd} (wait for ok)…")
            self._startup_home_update_controls()

            if not self._send(cmd, log=True, tag=tag, timeout_s=10.0, priority="high"):
                self._startup_home_pending_jobs = 0
                self._startup_home_motors_var.set("Motors: ?")
                self._startup_home_status_var.set("Failed to queue command (not connected).")
                self._startup_home_update_controls()

        motors_on_btn = ttk.Button(motors, text="Enable Motors (M17)", command=lambda: send_startup_motors(True))
        motors_on_btn.pack(side=tk.LEFT, padx=(10, 8))
        motors_off_btn = ttk.Button(motors, text="Disable Motors (M84)", command=lambda: send_startup_motors(False))
        motors_off_btn.pack(side=tk.LEFT)
        self._startup_home_buttons.extend([motors_on_btn, motors_off_btn])

        def send_startup_home(mode: str, axis: str) -> None:
            if self._worker is None or self._ser is None:
                return
            if self._startup_home_pending_jobs > 0:
                return
            if bool(getattr(self, "_kb_active", False)) or bool(getattr(self, "_kb_pending_start", False)):
                self._kb_stop()

            mode = mode.lower().strip()
            axis = axis.upper().strip()
            if mode not in {"auto", "manual"}:
                return
            if axis not in {"X", "Y", "Z", "ALL"}:
                return

            if mode == "auto" and axis == "Z":
                if self._startup_home_axis_status.get("X") != "auto" or self._startup_home_axis_status.get("Y") != "auto":
                    messagebox.showerror(
                        "Auto Z requires Auto XY",
                        "Auto-homing Z usually requires X and Y to be auto-homed first.\n\n"
                        "Run Auto X and Auto Y (or Auto All) first, or use Manual Z.",
                        parent=dlg,
                    )
                    return

            if mode == "auto":
                cmd = "G28" if axis == "ALL" else f"G28 {axis}"
                timeout = 180.0
            else:
                cmd = "G92 X0 Y0 Z0" if axis == "ALL" else f"G92 {axis}0"
                timeout = 10.0

            # Keep the UI strict: no other actions until we get ok.
            queued = 0
            if axis == "ALL":
                for a in ("X", "Y", "Z"):
                    self._startup_home_axis_status[a] = "pending"
                for a in ("X", "Y", "Z"):
                    self._startup_home_axis_vars[a].set(f"Running ({mode})…")
            else:
                self._startup_home_axis_status[axis] = "pending"
                self._startup_home_axis_vars[axis].set(f"Running ({mode})…")

            if mode == "auto":
                self._startup_home_status_var.set(f"Enabling motors (M17), then running: {cmd} (wait for ok)…")
                if self._send("M17", log=True, tag="startup_motors:on", timeout_s=10.0, priority="high"):
                    queued += 1
                if self._send(cmd, log=True, tag=f"startup_home:{mode}:{axis}", timeout_s=timeout, priority="high"):
                    queued += 1
            else:
                self._startup_home_status_var.set(f"Running: {cmd} (wait for ok)…")
                if self._send(cmd, log=True, tag=f"startup_home:{mode}:{axis}", timeout_s=timeout, priority="high"):
                    queued += 1

            self._startup_home_pending_jobs = queued
            self._startup_home_update_controls()

            if queued <= 0:
                self._startup_home_status_var.set("Failed to queue command (not connected).")
                if axis == "ALL":
                    for a in ("X", "Y", "Z"):
                        self._startup_home_axis_vars[a].set("Pending")
                else:
                    self._startup_home_axis_vars[axis].set("Pending")
                self._startup_home_update_controls()

        for i, axis in enumerate(("X", "Y", "Z"), start=0):
            r = 5 + i
            ttk.Label(body, text=axis, width=6).grid(row=r, column=0, sticky=tk.W, pady=(8, 0))
            ttk.Label(body, textvariable=self._startup_home_axis_vars[axis], width=16).grid(
                row=r, column=1, sticky=tk.W, padx=(10, 0), pady=(8, 0)
            )

            actions = ttk.Frame(body)
            actions.grid(row=r, column=2, columnspan=2, sticky=tk.W, padx=(10, 0), pady=(8, 0))
            auto_btn = ttk.Button(actions, text=f"Auto {axis}", command=lambda a=axis: send_startup_home("auto", a))
            auto_btn.pack(side=tk.LEFT, padx=(0, 8))
            manual_btn = ttk.Button(actions, text=f"Manual {axis}=0", command=lambda a=axis: send_startup_home("manual", a))
            manual_btn.pack(side=tk.LEFT)
            self._startup_home_buttons.extend([auto_btn, manual_btn])

        bottom = ttk.Frame(body)
        bottom.grid(row=8, column=0, columnspan=4, sticky=tk.E, pady=(14, 0))

        auto_all_btn = ttk.Button(bottom, text="Auto All", command=lambda: send_startup_home("auto", "ALL"))
        auto_all_btn.pack(side=tk.LEFT, padx=(0, 8))
        manual_all_btn = ttk.Button(bottom, text="Manual All", command=lambda: send_startup_home("manual", "ALL"))
        manual_all_btn.pack(side=tk.LEFT, padx=(0, 18))
        self._startup_home_buttons.extend([auto_all_btn, manual_all_btn])

        self._startup_home_continue_btn = ttk.Button(
            bottom,
            text="Continue",
            command=self._startup_home_continue,
            state="disabled",
        )
        self._startup_home_continue_btn.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(bottom, text="Disconnect", command=disconnect).pack(side=tk.LEFT)

        dlg.bind("<Escape>", lambda _e: disconnect())
        self._startup_home_update_controls()
