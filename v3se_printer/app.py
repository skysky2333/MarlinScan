from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import serial  # type: ignore

from .models import GCodeJob, PortItem, PrinterConfig, Priority
from .parsers import parse_m105, parse_m114, parse_m115, parse_m503
from .ports import list_serial_ports
from .serial_worker import SerialWorker
from .ui.bed import BedTabMixin
from .ui.maint import MaintTabMixin
from .ui.move import MoveTabMixin
from .ui.quick import QuickTabMixin
from .ui.realtime import RealtimeMixin
from .ui.temps import TempsTabMixin
from .ui.tuning import TuningTabMixin


def looks_like_marlin(lines: list[str]) -> bool:
    for line in lines:
        lower = line.lower()
        if "firmware_name" in lower or "marlin" in lower:
            return True
        if "machine_type" in lower and "ender" in lower:
            return True
    return False


def send_and_collect(
    ser: serial.Serial,
    command: str,
    *,
    eol: str,
    timeout_s: float,
) -> list[str]:
    command = command.strip()
    if not command:
        return []

    ending = "\r\n" if eol == "crlf" else "\n"
    ser.write((command + ending).encode("ascii", errors="ignore"))
    ser.flush()

    lines: list[str] = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        lines.append(text)
        lower = text.lower()
        if lower == "ok" or lower.startswith("ok ") or lower.startswith("error"):
            break
    return lines


class PrinterGUI(
    QuickTabMixin,
    MoveTabMixin,
    BedTabMixin,
    RealtimeMixin,
    TempsTabMixin,
    TuningTabMixin,
    MaintTabMixin,
    tk.Tk,
):
    def __init__(self) -> None:
        super().__init__()
        self.title("Ender-3 V3 SE Serial Control (Marlin)")
        self.minsize(1050, 640)

        self._ser: serial.Serial | None = None
        self._worker: SerialWorker | None = None
        self._events: queue.Queue[tuple[str, object]] = queue.Queue()

        self._config = PrinterConfig()
        self._poll_pending_m105 = False
        self._poll_pending_m114 = False
        self._home_prompt_pending = False
        self._home_prompt_seen_m105 = False
        self._home_prompt_seen_m114 = False
        self._home_prompt_shown = False
        self._deferred_m503 = False
        self._startup_home_dialog: tk.Toplevel | None = None
        self._startup_home_pending_jobs = 0
        self._startup_home_motors_enabled: bool | None = None
        self._startup_home_motors_var = tk.StringVar(value="Motors: ?")
        self._startup_home_axis_status: dict[str, str] = {"X": "pending", "Y": "pending", "Z": "pending"}
        self._startup_home_axis_vars: dict[str, tk.StringVar] = {
            "X": tk.StringVar(value="Pending"),
            "Y": tk.StringVar(value="Pending"),
            "Z": tk.StringVar(value="Pending"),
        }
        self._startup_home_status_var = tk.StringVar(value="")
        self._startup_home_continue_btn: ttk.Button | None = None
        self._startup_home_buttons: list[ttk.Button] = []

        self._port_items: list[PortItem] = []
        self._port_var = tk.StringVar()
        self._baud_var = tk.StringVar(value="115200")
        self._eol_var = tk.StringVar(value="crlf")
        self._status_var = tk.StringVar(value="Disconnected")
        self._command_var = tk.StringVar()

        self._firmware_var = tk.StringVar(value="Firmware: ?")
        self._pos_var = tk.StringVar(value="X:?  Y:?  Z:?  E:?")
        self._temp_var = tk.StringVar(value="Hotend:?/?°C  Bed:?/?°C")
        self._auto_poll_var = tk.BooleanVar(value=True)
        self._poll_interval_var = tk.StringVar(value="1.0")
        self._confirm_motion_var = tk.BooleanVar(value=True)
        self._bed_click_move_var = tk.BooleanVar(value=False)

        self._coord_mode_var = tk.StringVar(value="absolute")
        self._step_xy_var = tk.StringVar(value="10")
        self._step_z_var = tk.StringVar(value="1")
        self._max_speed_xy_var = tk.DoubleVar(value=500.0)  # mm/s (UI cap)
        self._max_speed_z_var = tk.DoubleVar(value=15.0)  # mm/s
        self._speed_xy_var = tk.DoubleVar(value=250.0)  # mm/s (default)
        self._speed_z_var = tk.DoubleVar(value=15.0)  # mm/s (default to max)

        self._abs_x_var = tk.StringVar()
        self._abs_y_var = tk.StringVar()
        self._abs_z_var = tk.StringVar()

        self._rel_x_var = tk.StringVar()
        self._rel_y_var = tk.StringVar()
        self._rel_z_var = tk.StringVar()

        self._extrude_amt_var = tk.StringVar(value="5")
        self._extrude_speed_var = tk.StringVar(value="5")  # mm/s

        self._hotend_target_var = tk.StringVar(value="0")
        self._bed_target_var = tk.StringVar(value="0")
        self._fan_var = tk.DoubleVar(value=0.0)  # 0..255

        self._feed_override_var = tk.IntVar(value=100)  # M220 S
        self._flow_override_var = tk.IntVar(value=100)  # M221 S
        self._accel_var = tk.DoubleVar(value=2500.0)  # M204 P/R/T (default)

        # Bed/work-area model (used for absolute positioning UI).
        # Defaults for Ender-3 V3 SE (commonly 220x220x250).
        self._bed_x_min_var = tk.DoubleVar(value=0.0)
        self._bed_x_max_var = tk.DoubleVar(value=220.0)
        self._bed_y_min_var = tk.DoubleVar(value=0.0)
        self._bed_y_max_var = tk.DoubleVar(value=220.0)
        self._bed_z_min_var = tk.DoubleVar(value=0.0)
        self._bed_z_max_var = tk.DoubleVar(value=250.0)

        self._target_x_var = tk.DoubleVar(value=0.0)
        self._target_y_var = tk.DoubleVar(value=0.0)
        self._target_z_var = tk.DoubleVar(value=0.0)
        self._z_raw_var = tk.DoubleVar(value=0.0)  # UI slider raw value (inverted to Z target)
        self._current_x: float | None = None
        self._current_y: float | None = None
        self._current_z: float | None = None

        # Realtime bed tracking (mouse → incremental jog streaming).
        self._rt_active = False
        self._rt_mouse_down = False
        self._rt_mouse_inside = False
        self._rt_virtual_x: float | None = None
        self._rt_virtual_y: float | None = None
        self._rt_pending_start = False
        self._rt_pending_acks = 0
        self._rt_restore_coord_mode: str | None = None

        self._rt_hold_mouse_var = tk.BooleanVar(value=True)
        self._rt_tick_hz_var = tk.StringVar(value="100")
        self._rt_step_mm_var = tk.StringVar(value="4")
        self._rt_deadband_mm_var = tk.StringVar(value="0.2")
        self._rt_sync_m400_var = tk.BooleanVar(value=False)
        self._rt_buffer_ms_var = tk.StringVar(value="30")
        self._rt_home_xy_var = tk.BooleanVar(value=False)
        self._rt_boost_motion_var = tk.BooleanVar(value=True)
        self._rt_boost_m201_xy_var = tk.StringVar(value="3000")
        self._rt_boost_m204_var = tk.StringVar(value="3000")
        self._rt_boost_junction_var = tk.StringVar(value="0.20")
        self._rt_boost_applied = False
        self._rt_boost_saved_m201_xy: tuple[float | None, float | None] | None = None
        self._rt_boost_saved_m204: float | None = None
        self._rt_boost_saved_junction: float | None = None

        self._rt_target_x_var = tk.DoubleVar(value=0.0)
        self._rt_target_y_var = tk.DoubleVar(value=0.0)
        self._rt_status_var = tk.StringVar(value="Stopped")
        self._rt_queue_time_s = 0.0
        self._rt_last_tick_time: float | None = None

        # Realtime keyboard jog (arrow keys → XY, Shift/Ctrl → Z).
        self._kb_active = False
        self._kb_pending_start = False
        self._kb_pending_acks = 0
        self._kb_restore_coord_mode: str | None = None
        self._kb_virtual_x: float | None = None
        self._kb_virtual_y: float | None = None
        self._kb_virtual_z: float | None = None
        self._kb_keys_down: set[str] = set()
        self._kb_queue_time_s = 0.0
        self._kb_last_tick_time: float | None = None

        self._kb_tick_hz_var = tk.StringVar(value="100")
        self._kb_step_xy_mm_var = tk.StringVar(value="4")
        self._kb_step_z_mm_var = tk.StringVar(value="0.5")
        self._kb_sync_m400_var = tk.BooleanVar(value=False)
        self._kb_buffer_ms_var = tk.StringVar(value="30")
        self._kb_status_var = tk.StringVar(value="Stopped")

        # Internal binding/paint bookkeeping for realtime modes.
        self._kb_bind_press_id: str | None = None
        self._kb_bind_release_id: str | None = None
        self._rt_redraw_after_id: str | None = None
        self._rt_last_redraw_time: float | None = None

        self._build_ui()
        self._set_controls_connected(False)
        self.refresh_ports()

        self.after(50, self._drain_events)
        self.after(500, self._poll_tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=10)
        top.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(top, text="Port:").grid(row=0, column=0, sticky=tk.W)
        self.port_combo = ttk.Combobox(top, textvariable=self._port_var, width=55, state="readonly")
        self.port_combo.grid(row=0, column=1, sticky=tk.W, padx=(6, 10))

        self.refresh_btn = ttk.Button(top, text="Refresh", command=self.refresh_ports)
        self.refresh_btn.grid(row=0, column=2, sticky=tk.W)

        ttk.Label(top, text="Baud:").grid(row=0, column=3, sticky=tk.W, padx=(16, 0))
        self.baud_combo = ttk.Combobox(
            top,
            textvariable=self._baud_var,
            width=10,
            values=["115200", "250000", "230400", "57600", "38400"],
        )
        self.baud_combo.grid(row=0, column=4, sticky=tk.W, padx=(6, 10))

        ttk.Label(top, text="EOL:").grid(row=0, column=5, sticky=tk.W)
        self.eol_combo = ttk.Combobox(
            top,
            textvariable=self._eol_var,
            width=8,
            values=["crlf", "lf"],
            state="readonly",
        )
        self.eol_combo.grid(row=0, column=6, sticky=tk.W, padx=(6, 10))

        self.connect_btn = ttk.Button(top, text="Connect", command=self.toggle_connect)
        self.connect_btn.grid(row=0, column=7, sticky=tk.W)

        self.detect_btn = ttk.Button(top, text="Auto-detect (find port/baud)", command=self.auto_detect)
        self.detect_btn.grid(row=0, column=8, sticky=tk.W, padx=(10, 0))

        ttk.Label(top, textvariable=self._status_var).grid(row=1, column=0, columnspan=9, sticky=tk.W, pady=(8, 0))
        ttk.Label(top, textvariable=self._firmware_var).grid(row=2, column=0, columnspan=9, sticky=tk.W)

        for col in range(9):
            top.grid_columnconfigure(col, weight=0)
        top.grid_columnconfigure(1, weight=1)

        body = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        controls = ttk.Frame(body)
        console = ttk.Frame(body)
        body.add(controls, weight=1)
        body.add(console, weight=2)

        status = ttk.LabelFrame(controls, text="Status", padding=10)
        status.pack(side=tk.TOP, fill=tk.X, pady=(0, 10))

        ttk.Label(status, text="Position:").grid(row=0, column=0, sticky=tk.W)
        ttk.Label(status, textvariable=self._pos_var).grid(row=0, column=1, sticky=tk.W, padx=(8, 0))

        ttk.Label(status, text="Temps:").grid(row=1, column=0, sticky=tk.W, pady=(4, 0))
        ttk.Label(status, textvariable=self._temp_var).grid(row=1, column=1, sticky=tk.W, padx=(8, 0), pady=(4, 0))

        ttk.Button(status, text="Query M105", command=lambda: self._send("M105")).grid(
            row=2, column=0, sticky=tk.W, pady=(10, 0)
        )
        ttk.Button(status, text="Query M114", command=lambda: self._send("M114")).grid(
            row=2, column=1, sticky=tk.W, padx=(8, 0), pady=(10, 0)
        )

        ttk.Checkbutton(status, text="Auto refresh", variable=self._auto_poll_var).grid(
            row=3, column=0, sticky=tk.W, pady=(8, 0)
        )
        ttk.Label(status, text="Interval (s):").grid(row=3, column=1, sticky=tk.W, padx=(8, 0), pady=(8, 0))
        ttk.Entry(status, textvariable=self._poll_interval_var, width=6).grid(
            row=3, column=2, sticky=tk.W, padx=(6, 0), pady=(8, 0)
        )

        ttk.Checkbutton(status, text="Confirm moves", variable=self._confirm_motion_var).grid(
            row=4, column=0, sticky=tk.W, pady=(8, 0)
        )

        status.grid_columnconfigure(1, weight=1)

        self.notebook = ttk.Notebook(controls)
        self.notebook.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        quick_tab = ttk.Frame(self.notebook, padding=10)
        move_tab = ttk.Frame(self.notebook, padding=10)
        bed_tab = ttk.Frame(self.notebook, padding=10)
        bed_realtime_tab = ttk.Frame(self.notebook, padding=10)
        temps_tab = ttk.Frame(self.notebook, padding=10)
        tuning_tab = ttk.Frame(self.notebook, padding=10)
        maint_tab = ttk.Frame(self.notebook, padding=10)

        self.notebook.add(quick_tab, text="Quick")
        self.notebook.add(move_tab, text="Move")
        self.notebook.add(bed_tab, text="Bed")
        self.notebook.add(bed_realtime_tab, text="Bed Realtime")
        self.notebook.add(temps_tab, text="Temps/Fan")
        self.notebook.add(tuning_tab, text="Tuning")
        self.notebook.add(maint_tab, text="Level/EEPROM")

        self._build_quick_tab(quick_tab)
        self._build_move_tab(move_tab)
        self._build_bed_tab(bed_tab)
        self._build_bed_realtime_tab(bed_realtime_tab)
        self._build_temps_tab(temps_tab)
        self._build_tuning_tab(tuning_tab)
        self._build_maint_tab(maint_tab)

        self.log = scrolledtext.ScrolledText(console, height=20, wrap=tk.WORD, state="disabled")
        self.log.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        bottom = ttk.Frame(self, padding=(10, 0, 10, 10))
        bottom.pack(side=tk.BOTTOM, fill=tk.X)

        ttk.Label(bottom, text="G-code:").grid(row=0, column=0, sticky=tk.W)
        self.command_entry = ttk.Entry(bottom, textvariable=self._command_var, width=70)
        self.command_entry.grid(row=0, column=1, sticky=tk.W, padx=(6, 10))
        self.command_entry.bind("<Return>", lambda _e: self.send_command())

        self.send_btn = ttk.Button(bottom, text="Send", command=self.send_command)
        self.send_btn.grid(row=0, column=2, sticky=tk.W)

        self.clear_btn = ttk.Button(bottom, text="Clear Log", command=self.clear_log)
        self.clear_btn.grid(row=0, column=3, sticky=tk.W, padx=(10, 0))

        bottom.grid_columnconfigure(1, weight=1)

    def _poll_tick(self) -> None:
        try:
            interval_s = float(self._poll_interval_var.get().strip())
        except Exception:
            interval_s = 2.0
        interval_s = max(0.5, min(30.0, interval_s))

        if self._ser is not None and bool(self._auto_poll_var.get()):
            if self._startup_home_dialog is not None:
                # Don't enqueue background polls while the startup dialog is active.
                # This keeps homing/setup actions snappy and avoids interleaving.
                self.after(int(interval_s * 1000), self._poll_tick)
                return
            if self._rt_active or self._kb_active:
                # Avoid injecting M105/M114 while realtime motion streaming is active.
                self.after(int(interval_s * 1000), self._poll_tick)
                return
            # Status polling: enqueue low-priority queries (one-at-a-time).
            if (not self._poll_pending_m105) and self._send(
                "M105",
                log=False,
                priority="low",
                tag="poll_m105",
                timeout_s=3.0,
                interactive=False,
            ):
                self._poll_pending_m105 = True
            if (not self._poll_pending_m114) and self._send(
                "M114",
                log=False,
                priority="low",
                tag="poll_m114",
                timeout_s=3.0,
                interactive=False,
            ):
                self._poll_pending_m114 = True

        self.after(int(interval_s * 1000), self._poll_tick)

    def _handle_incoming_line(self, line: str) -> None:
        fw_info = parse_m115(line)
        if fw_info is not None:
            fw, machine = fw_info
            if fw and machine:
                self._firmware_var.set(f"Firmware: {fw} | Machine: {machine}")
            elif fw:
                self._firmware_var.set(f"Firmware: {fw}")
            elif machine:
                self._firmware_var.set(f"Machine: {machine}")

        temps = parse_m105(line)
        if temps is not None:
            hotend_now, hotend_target, bed_now, bed_target = temps
            hotend = "?" if hotend_now is None else f"{hotend_now:.1f}"
            hotend_t = "?" if hotend_target is None else f"{hotend_target:.1f}"
            bed = "?" if bed_now is None else f"{bed_now:.1f}"
            bed_t = "?" if bed_target is None else f"{bed_target:.1f}"
            self._temp_var.set(f"Hotend:{hotend}/{hotend_t}°C  Bed:{bed}/{bed_t}°C")

        if not line.lstrip().lower().startswith("count"):
            pos = parse_m114(line)
            if pos is not None:
                x, y, z, e = pos
                if x is None or y is None or z is None:
                    return
                self._current_x, self._current_y, self._current_z = x, y, z
                e_text = "?" if e is None else f"{e:.2f}"
                self._pos_var.set(f"X:{x:.2f}  Y:{y:.2f}  Z:{z:.2f}  E:{e_text}")
                self._update_bed_status()
                self._redraw_bed()
                self._redraw_rt_bed()

    def _handle_job_done(
        self,
        job: GCodeJob,
        lines: list[str],
        ok: bool,
        elapsed_s: float,
        err: str | None,
    ) -> None:
        if job.tag == "m115" and ok:
            # Ensure firmware is extracted even if line events are missed.
            for line in lines:
                self._handle_incoming_line(line)

        # Realtime-specific state updates (shared across realtime modes).
        self._realtime_handle_job_done(job, lines, ok)

        if job.tag == "poll_m105":
            self._poll_pending_m105 = False
            if ok:
                if self._home_prompt_pending and (not self._home_prompt_seen_m105):
                    self._home_prompt_seen_m105 = True
                for line in lines:
                    temps = parse_m105(line)
                    if temps is None:
                        continue
                    hotend_now, hotend_target, bed_now, bed_target = temps
                    hotend = "?" if hotend_now is None else f"{hotend_now:.1f}"
                    hotend_t = "?" if hotend_target is None else f"{hotend_target:.1f}"
                    bed = "?" if bed_now is None else f"{bed_now:.1f}"
                    bed_t = "?" if bed_target is None else f"{bed_target:.1f}"
                    self._temp_var.set(f"Hotend:{hotend}/{hotend_t}°C  Bed:{bed}/{bed_t}°C")
                    break
        elif job.tag == "poll_m114":
            self._poll_pending_m114 = False
            if ok:
                if self._home_prompt_pending and (not self._home_prompt_seen_m114):
                    self._home_prompt_seen_m114 = True
                for line in lines:
                    if line.lstrip().lower().startswith("count"):
                        continue
                    pos = parse_m114(line)
                    if pos is None:
                        continue
                    x, y, z, e = pos
                    if x is None or y is None or z is None:
                        continue
                    self._current_x, self._current_y, self._current_z = x, y, z
                    e_text = "?" if e is None else f"{e:.2f}"
                    self._pos_var.set(f"X:{x:.2f}  Y:{y:.2f}  Z:{z:.2f}  E:{e_text}")
                    self._update_bed_status()
                    self._redraw_bed()
                    self._redraw_rt_bed()
                    break

        if job.tag.startswith("startup_home:") or job.tag.startswith("startup_motors:"):
            self._startup_home_handle_job_done(job, ok)

        self._maybe_show_home_prompt()

        if job.tag == "m503" and ok:
            self._apply_m503(lines)

        if (not ok) and err and job.tag not in {"poll_m105", "poll_m114"}:
            self._append_log(f"[{err}] {job.command} ({elapsed_s:.1f}s)")

    def _apply_m503(self, lines: list[str]) -> None:
        old_max_z = float(self._max_speed_z_var.get())
        old_speed_z = float(self._speed_z_var.get())

        max_feed, accel_p, max_accel, junction_dev = parse_m503(lines)
        if max_feed:
            self._config.max_feedrate_mm_s.update(max_feed)

            z = self._config.max_feedrate_mm_s.get("Z")

            if z:
                self._max_speed_z_var.set(max(0.5, z))

        if accel_p is not None:
            self._config.accel_print_mm_s2 = accel_p
            self._accel_var.set(float(int(round(accel_p))))
            self._update_accel_label()

        if max_accel:
            self._config.max_accel_mm_s2.update(max_accel)

        if junction_dev is not None:
            self._config.junction_deviation = junction_dev

        # Keep the Z speed at max when the user hasn't changed it.
        new_max_z = float(self._max_speed_z_var.get())
        if abs(old_speed_z - old_max_z) < 1e-6:
            self._speed_z_var.set(new_max_z)

        self._apply_speed_limits()

        if max_feed or (accel_p is not None) or max_accel or (junction_dev is not None):
            x_v = self._config.max_feedrate_mm_s.get("X")
            y_v = self._config.max_feedrate_mm_s.get("Y")
            z_v = self._config.max_feedrate_mm_s.get("Z")
            msg = f"[config] Firmware max axis feedrates (M203, mm/s): X={x_v} Y={y_v} Z={z_v}"
            if accel_p is not None:
                msg += f" | Accel P={accel_p}"
            if max_accel:
                ax = self._config.max_accel_mm_s2.get("X")
                ay = self._config.max_accel_mm_s2.get("Y")
                msg += f" | Max accel (M201, mm/s^2): X={ax} Y={ay}"
            if junction_dev is not None:
                msg += f" | Junction dev (M205 J)={junction_dev}"
            self._append_log(msg)

    @staticmethod
    def _mm_s_to_mm_min(mm_s: float) -> int:
        return int(round(mm_s * 60.0))

    @staticmethod
    def _float_or_none(text: str) -> float | None:
        text = text.strip()
        if not text:
            return None
        return float(text)

    def _send(
        self,
        cmd: str,
        *,
        log: bool = True,
        priority: Priority = "high",
        tag: str = "",
        timeout_s: float | None = None,
        interactive: bool = True,
    ) -> bool:
        cmd = cmd.strip()
        if not cmd:
            return False
        if self._worker is None:
            if interactive:
                messagebox.showerror("Send", "Not connected.")
            return False

        if log:
            self._append_log(f"> {cmd}")

        return self._worker.enqueue(
            cmd,
            tag=tag,
            show_in_log=log,
            priority=priority,
            timeout_s=timeout_s,
        )

    def _send_many(self, cmds: list[tuple[str, bool]]) -> None:
        for cmd, log in cmds:
            self._send(cmd, log=log)

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert(tk.END, text + "\n")
        self.log.see(tk.END)
        self.log.configure(state="disabled")

    def clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", tk.END)
        self.log.configure(state="disabled")

    def refresh_ports(self) -> None:
        items = list_serial_ports(include_dialin=True)
        self._port_items = items
        values = [it.label for it in items]
        self.port_combo["values"] = values

        # Keep existing selection if still present; otherwise choose the first non-Bluetooth port.
        current = self._port_var.get().strip()
        if current and current in values:
            return
        if items:
            def score(it: PortItem) -> int:
                dev = it.device.lower()
                label = it.label.lower()
                if "bluetooth" in dev or "bluetooth" in label:
                    return -999
                s = 0
                if dev.startswith("/dev/tty.wchusbserial"):
                    s += 300
                elif dev.startswith("/dev/cu.wchusbserial"):
                    s += 250
                elif dev.startswith("/dev/tty.usbserial"):
                    s += 200
                elif dev.startswith("/dev/cu.usbserial"):
                    s += 180

                if "1a86:7523" in label:
                    s += 100
                if dev.startswith("/dev/tty."):
                    s += 10
                if dev.startswith("/dev/cu."):
                    s += 5
                return s

            best = max(items, key=score)
            if score(best) > -999:
                self._port_var.set(best.label)
                return
        if values:
            self._port_var.set(values[0])

    def _set_controls_connected(self, connected: bool) -> None:
        self.port_combo.configure(state="disabled" if connected else "readonly")
        self.baud_combo.configure(state="disabled" if connected else "normal")
        self.eol_combo.configure(state="disabled" if connected else "readonly")
        self.refresh_btn.configure(state="disabled" if connected else "normal")
        self.detect_btn.configure(state="disabled" if connected else "normal")

        self.connect_btn.configure(text="Disconnect" if connected else "Connect")
        self.send_btn.configure(state="normal" if connected else "disabled")
        self.command_entry.configure(state="normal" if connected else "disabled")

    def _resolve_port(self) -> str:
        value = self._port_var.get().strip()
        if not value:
            return ""
        for it in self._port_items:
            if it.label == value:
                return it.device
        return value

    def _set_port_by_device(self, device: str) -> None:
        for it in self._port_items:
            if it.device == device:
                self._port_var.set(it.label)
                return
        self._port_var.set(device)

    def toggle_connect(self) -> None:
        if self._ser is not None:
            self.disconnect()
        else:
            self.connect()

    def connect(self) -> None:
        port = self._resolve_port()
        if not port:
            messagebox.showerror("Connect", "Please select a serial port.")
            return

        try:
            baud = int(self._baud_var.get().strip())
        except ValueError:
            messagebox.showerror("Connect", "Invalid baud rate.")
            return

        self._append_log(f"[connect] Opening {port} @ {baud} (8N1, no flow control)")
        try:
            ser = serial.Serial(
                port=port,
                baudrate=baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.2,
                write_timeout=2,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
        except serial.SerialException as exc:
            messagebox.showerror("Connect", f"Failed to open {port}:\n{exc}")
            return

        # Many Marlin boards reset on connect.
        time.sleep(2.0)
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        self._ser = ser
        self._worker = SerialWorker(ser, eol=(self._eol_var.get().strip() or "crlf"))
        self._worker.start()

        self._status_var.set(f"Connected: {port} @ {baud} (EOL={self._eol_var.get()})")
        self._set_controls_connected(True)
        self._poll_pending_m105 = False
        self._poll_pending_m114 = False
        self._home_prompt_pending = True
        self._home_prompt_seen_m105 = False
        self._home_prompt_seen_m114 = False
        self._home_prompt_shown = False
        self._startup_home_axis_status = {"X": "pending", "Y": "pending", "Z": "pending"}
        for axis in ("X", "Y", "Z"):
            self._startup_home_axis_vars[axis].set("Pending")
        self._startup_home_status_var.set("")
        self._startup_home_continue_btn = None
        self._startup_home_buttons = []
        self._startup_home_pending_jobs = 0
        self._startup_home_motors_enabled = None
        self._startup_home_motors_var.set("Motors: ?")
        self._deferred_m503 = True

        # Initial handshake and config load.
        self._send("M115", log=False, tag="m115", timeout_s=5.0, interactive=False)

        # Prime UI quickly, then fetch config (M503 can take a while).
        if self._send("M105", log=False, tag="poll_m105", priority="high", timeout_s=3.0, interactive=False):
            self._poll_pending_m105 = True
        if self._send("M114", log=False, tag="poll_m114", priority="high", timeout_s=3.0, interactive=False):
            self._poll_pending_m114 = True

        # Defer M503 until after startup homing prompt so homing buttons run immediately.

    def disconnect(self) -> None:
        if self._ser is None:
            return

        # Stop realtime streaming first (avoid scheduling more commands while closing the port).
        self._realtime_cleanup_on_disconnect()

        self._append_log("[disconnect] Closing serial port")
        if self._worker is not None:
            self._worker.clear_queues()
            self._worker.close()
            self._worker = None
        self._ser = None
        self._status_var.set("Disconnected")
        self._firmware_var.set("Firmware: ?")
        self._pos_var.set("X:?  Y:?  Z:?  E:?")
        self._temp_var.set("Hotend:?/?°C  Bed:?/?°C")
        self._set_controls_connected(False)
        self._poll_pending_m105 = False
        self._poll_pending_m114 = False
        self._home_prompt_pending = False
        self._home_prompt_seen_m105 = False
        self._home_prompt_seen_m114 = False
        self._home_prompt_shown = False
        self._deferred_m503 = False
        if self._startup_home_dialog is not None:
            try:
                self._startup_home_dialog.destroy()
            except Exception:
                pass
            self._startup_home_dialog = None
        self._startup_home_axis_status = {"X": "pending", "Y": "pending", "Z": "pending"}
        for axis in ("X", "Y", "Z"):
            self._startup_home_axis_vars[axis].set("Pending")
        self._startup_home_status_var.set("")
        self._startup_home_continue_btn = None
        self._startup_home_buttons = []
        self._startup_home_pending_jobs = 0
        self._startup_home_motors_enabled = None
        self._startup_home_motors_var.set("Motors: ?")
        self._current_x = None
        self._current_y = None
        self._current_z = None
        if hasattr(self, "_bed_status_var"):
            self._update_bed_status()
        self._redraw_bed()
        self._redraw_rt_bed()

    def _drain_events(self) -> None:
        if self._worker is not None:
            try:
                while True:
                    kind, payload = self._worker.events.get_nowait()
                    if kind == "line":
                        line, show = payload  # type: ignore[misc]
                        text = str(line)
                        self._handle_incoming_line(text)
                        if bool(show):
                            self._append_log(text)
                    elif kind == "error":
                        self._append_log(f"[error] {payload}")
                        self.disconnect()
                    elif kind == "job_done":
                        job, lines, ok, elapsed = payload  # type: ignore[misc]
                        self._handle_job_done(job, list(lines), bool(ok), float(elapsed), None)
                    elif kind == "job_slow":
                        job, elapsed = payload  # type: ignore[misc]
                        self._append_log(f"[waiting] {job.command} ({float(elapsed):.1f}s)")
            except queue.Empty:
                pass

        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "detect":
                    if isinstance(payload, tuple) and len(payload) == 4:
                        port, baud, eol, sample = payload  # type: ignore[misc]
                        self._eol_var.set(str(eol))
                    else:
                        port, baud, sample = payload  # type: ignore[misc]
                    self._set_port_by_device(str(port))
                    self._baud_var.set(str(baud))
                    self._append_log(f"[detect] Found printer on {port} @ {baud}")
                    for line in sample:
                        self._handle_incoming_line(line)
                        self._append_log(line)
                    self._status_var.set(f"Auto-detect OK: {port} @ {baud}")
                elif kind == "detect-log":
                    self._append_log(str(payload))
                elif kind == "detect-none":
                    self._append_log("[detect] No working port/baud found.")
                    self._status_var.set("Auto-detect failed (try different cable/driver/baud).")
        except queue.Empty:
            pass
        self.after(50, self._drain_events)

    def send_command(self) -> None:
        cmd = self._command_var.get().strip()
        if not cmd:
            return
        self._send(cmd, log=True)
        self._command_var.set("")

    def send_macro(self, cmd: str) -> None:
        self._send(cmd, log=True)

    def estop_confirmed(self) -> None:
        if self._worker is None:
            messagebox.showerror("E-Stop", "Not connected.")
            return
        ok = messagebox.askokcancel(
            "EMERGENCY STOP (M112)",
            "This triggers an emergency stop (M112).\n\nContinue?",
        )
        if ok:
            # M112 must bypass normal queuing; it should be sent immediately.
            self._worker.clear_queues()
            self._worker.send_immediate("M112")

    def auto_detect(self) -> None:
        if self._ser is not None:
            messagebox.showinfo("Auto-detect", "Disconnect first.")
            return

        self.refresh_ports()
        ports = [it.device for it in self._port_items if "bluetooth" not in it.device.lower()]
        bauds = []
        for b in ["115200", "250000", "230400", "128000", "150000", "57600"]:
            try:
                bauds.append(int(b))
            except ValueError:
                continue

        eol = self._eol_var.get()
        self._status_var.set("Auto-detect running…")
        self._append_log("[detect] Probing ports…")

        t = threading.Thread(
            target=self._auto_detect_worker,
            args=(ports, bauds, eol),
            daemon=True,
        )
        t.start()

    def _auto_detect_worker(self, ports: list[str], bauds: list[int], eol: str) -> None:
        # Try the chosen EOL first, then the other one for robustness.
        eols = [eol]
        if eol == "crlf":
            eols.append("lf")
        elif eol == "lf":
            eols.append("crlf")
        else:
            eols.extend(["crlf", "lf"])

        for port in ports:
            for baud in bauds:
                self._events.put(("detect-log", f"[detect] Trying {port} @ {baud}…"))
                try:
                    with serial.Serial(
                        port=port,
                        baudrate=baud,
                        bytesize=serial.EIGHTBITS,
                        parity=serial.PARITY_NONE,
                        stopbits=serial.STOPBITS_ONE,
                        timeout=0.2,
                        write_timeout=2,
                        xonxoff=False,
                        rtscts=False,
                        dsrdtr=False,
                    ) as ser:
                        time.sleep(2.5)
                        ser.reset_input_buffer()
                        ser.reset_output_buffer()

                        for eol_try in eols:
                            lines = send_and_collect(ser, "M115", eol=eol_try, timeout_s=3.5)
                            if looks_like_marlin(lines):
                                self._events.put(("detect", (port, baud, eol_try, lines)))
                                return
                except serial.SerialException as exc:
                    self._events.put(("detect-log", f"[detect] {port} @ {baud} failed: {exc}"))
                    continue
        self._events.put(("detect-none", None))

    def _on_close(self) -> None:
        try:
            self.disconnect()
        finally:
            self.destroy()


def main() -> int:
    app = PrinterGUI()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

