from __future__ import annotations

import math
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, simpledialog, ttk

import serial  # type: ignore

from .models import GCodeJob, PortItem, PrinterConfig, Priority
from .parsers import parse_m105, parse_m114, parse_m115, parse_m503
from .ports import list_serial_ports
from .serial_worker import SerialWorker


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


class PrinterGUI(tk.Tk):
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

        ttk.Label(top, textvariable=self._status_var).grid(
            row=1, column=0, columnspan=9, sticky=tk.W, pady=(8, 0)
        )
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

    def _build_quick_tab(self, parent: ttk.Frame) -> None:
        info = ttk.LabelFrame(parent, text="Info / Status", padding=10)
        info.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(info, text="M115 (Info)", command=lambda: self._send("M115")).grid(
            row=0, column=0, sticky=tk.W, padx=(0, 6), pady=(0, 6)
        )
        ttk.Button(info, text="M105 (Temps)", command=lambda: self._send("M105")).grid(
            row=0, column=1, sticky=tk.W, padx=(0, 6), pady=(0, 6)
        )
        ttk.Button(info, text="M114 (Pos)", command=lambda: self._send("M114")).grid(
            row=0, column=2, sticky=tk.W, padx=(0, 6), pady=(0, 6)
        )
        ttk.Button(info, text="M119 (Endstops)", command=lambda: self._send("M119")).grid(
            row=0, column=3, sticky=tk.W, pady=(0, 6)
        )
        ttk.Button(info, text="M503 (Report)", command=lambda: self._send("M503")).grid(
            row=1, column=0, sticky=tk.W
        )

        motion = ttk.LabelFrame(parent, text="Motion / Safety", padding=10)
        motion.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))

        ttk.Button(motion, text="Motors On (M17)", command=lambda: self._send("M17")).grid(
            row=0, column=0, sticky=tk.W, padx=(0, 6), pady=(0, 6)
        )
        ttk.Button(motion, text="Motors Off (M84)", command=lambda: self._send("M84")).grid(
            row=0, column=1, sticky=tk.W, padx=(0, 6), pady=(0, 6)
        )
        ttk.Button(motion, text="Home All (G28)", command=lambda: self.home(None)).grid(
            row=0, column=2, sticky=tk.W, padx=(0, 6), pady=(0, 6)
        )
        ttk.Button(motion, text="Auto Level (G29)", command=self.auto_level_confirmed).grid(
            row=0, column=3, sticky=tk.W, pady=(0, 6)
        )

        ttk.Button(motion, text="EMERGENCY STOP (M112)", command=self.estop_confirmed).grid(
            row=1, column=0, sticky=tk.W, padx=(0, 6)
        )
        ttk.Button(motion, text="Reset (M999)", command=lambda: self._send("M999")).grid(
            row=1, column=1, sticky=tk.W, padx=(0, 6)
        )
        ttk.Button(motion, text="Mesh On (M420 S1)", command=lambda: self._send("M420 S1")).grid(
            row=1, column=2, sticky=tk.W, padx=(0, 6)
        )
        ttk.Button(motion, text="Mesh Off (M420 S0)", command=lambda: self._send("M420 S0")).grid(
            row=1, column=3, sticky=tk.W
        )

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
        ttk.Button(buttons, text="Move To Target", command=self.move_to_target_confirmed).pack(side=tk.RIGHT, padx=(6, 0))

        self._update_speed_labels()
        self._sync_z_slider_from_target()
        self._redraw_bed()

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

    def _rt_target_from_current(self) -> None:
        if self._current_x is None or self._current_y is None:
            return
        x_min, x_max, y_min, y_max, _z_min, _z_max = self._bed_bounds()
        self._rt_target_x_var.set(self._clamp(float(self._current_x), x_min, x_max))
        self._rt_target_y_var.set(self._clamp(float(self._current_y), y_min, y_max))
        if self._rt_virtual_x is None or self._rt_virtual_y is None:
            self._rt_virtual_x = float(self._current_x)
            self._rt_virtual_y = float(self._current_y)
        self._redraw_rt_bed()

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
        self._redraw_rt_bed()

    def _on_rt_leave(self, _event: tk.Event) -> None:
        self._rt_mouse_inside = False
        self._rt_mouse_down = False
        self._redraw_rt_bed()

    def _on_rt_press(self, event: tk.Event) -> None:
        self._rt_mouse_down = True
        self._on_rt_motion(event)

    def _on_rt_release(self, _event: tk.Event) -> None:
        self._rt_mouse_down = False
        self._redraw_rt_bed()

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
        self._redraw_rt_bed()

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

        self._redraw_rt_bed()

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
            self._rt_status_var.set(f"Running (backlog {self._rt_pending_acks}, q≈{self._rt_queue_time_s*1000:.0f}ms)")
            self.after(interval_ms, self._rt_tick)
            return

        if (
            (not should_move)
            or self._rt_virtual_x is None
            or self._rt_virtual_y is None
        ):
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

        self._redraw_rt_bed()
        self.after(interval_ms, self._rt_tick)

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
        ox, oy, size, w, h = self._bed_canvas_square()

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

    def _build_temps_tab(self, parent: ttk.Frame) -> None:
        now = ttk.LabelFrame(parent, text="Current", padding=10)
        now.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(now, textvariable=self._temp_var).pack(side=tk.LEFT)
        ttk.Button(now, text="Query (M105)", command=lambda: self._send("M105")).pack(side=tk.RIGHT)

        setpoints = ttk.LabelFrame(parent, text="Setpoints", padding=10)
        setpoints.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))

        ttk.Label(setpoints, text="Hotend (M104):").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(setpoints, textvariable=self._hotend_target_var, width=6).grid(row=0, column=1, sticky=tk.W, padx=(6, 12))
        ttk.Button(setpoints, text="Set", command=self.apply_hotend_target).grid(row=0, column=2, sticky=tk.W, padx=(0, 12))

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

    def _update_fan_label(self) -> None:
        try:
            value = int(round(float(self._fan_var.get())))
        except Exception:
            value = 0
        value = max(0, min(255, value))
        pct = int(round(value * 100 / 255))
        self.fan_label.configure(text=f"{pct}%")

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
            if self._rt_active:
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

        if job.tag in {"rt_move", "rt_m400"} and self._rt_pending_acks > 0:
            self._rt_pending_acks -= 1
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
                    self._redraw_rt_bed()
                    break

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

        if job.tag.startswith("startup_home:"):
            self._startup_home_handle_job_done(job, ok)
        elif job.tag.startswith("startup_motors:"):
            self._startup_home_handle_job_done(job, ok)

        self._maybe_show_home_prompt()

        if job.tag == "m503" and ok:
            self._apply_m503(lines)

        if (not ok) and err and job.tag not in {"poll_m105", "poll_m114"}:
            self._append_log(f"[{err}] {job.command} ({elapsed_s:.1f}s)")

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

        busy = self._startup_home_pending_jobs > 0
        for btn in self._startup_home_buttons:
            try:
                btn.configure(state=("disabled" if busy else "normal"))
            except Exception:
                continue

        done = all(self._startup_home_axis_status.get(a) in {"auto", "manual"} for a in ("X", "Y", "Z"))
        if self._startup_home_continue_btn is not None:
            self._startup_home_continue_btn.configure(
                state=("normal" if (done and (not busy)) else "disabled")
            )
        if done and (not busy):
            self._startup_home_status_var.set("All axes are set. Click Continue.")

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

        hdr = ttk.Frame(body)
        hdr.grid(row=3, column=0, columnspan=4, sticky=tk.W, pady=(10, 0))
        ttk.Label(hdr, text="Axis", width=6).grid(row=0, column=0, sticky=tk.W)
        ttk.Label(hdr, text="Status", width=16).grid(row=0, column=1, sticky=tk.W, padx=(10, 0))
        ttk.Label(hdr, text="Actions").grid(row=0, column=2, sticky=tk.W, padx=(10, 0))

        self._startup_home_buttons = []

        def send_startup_motors(enable: bool) -> None:
            if self._worker is None or self._ser is None:
                return
            if self._startup_home_pending_jobs > 0:
                return

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
                cmd = (
                    "G92 X0 Y0 Z0"
                    if axis == "ALL"
                    else f"G92 {axis}0"
                )
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
            r = 4 + i
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
        bottom.grid(row=7, column=0, columnspan=4, sticky=tk.E, pady=(14, 0))

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
        self._rt_active = False
        self._rt_pending_start = False
        self._rt_pending_acks = 0
        self._rt_mouse_down = False
        self._rt_mouse_inside = False
        self._rt_status_var.set("Stopped (disconnected)")
        self._rt_restore_motion_boost()
        if hasattr(self, "rt_start_btn"):
            self.rt_start_btn.configure(state="normal")
        if hasattr(self, "rt_stop_btn"):
            self.rt_stop_btn.configure(state="disabled")

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

    def home_confirmed(self) -> None:
        # Backwards-compat button handler (older UI); keep it wired to new homing logic.
        self.home(None)

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
