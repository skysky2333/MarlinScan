from __future__ import annotations

from collections import deque
import base64
from dataclasses import replace
import os
import queue
import re
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
from .ui.scan import ScanTabMixin
from .ui.temps import TempsTabMixin
from .ui.tuning import TuningTabMixin
from .uvc import (
    UvcCameraConfig,
    apply_uvc_config,
    compute_sharpness,
    get_capture_info,
    probe_uvc_indices,
    transform_frame,
)


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
    ScanTabMixin,
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

        # Background "wait for command done" helpers (used by camera/printer coordination).
        self._job_waiters_lock = threading.Lock()
        self._job_waiters: dict[str, threading.Event] = {}
        self._job_waiter_results: dict[str, tuple[bool, list[str]]] = {}
        self._job_waiter_seq = 0

        # UVC camera state (optional, requires opencv-python).
        self._cam_lock = threading.Lock()
        self._cam_cap: object | None = None
        self._cam_connected = False
        self._cam_connecting = False
        self._cam_scanning = False
        self._cam_worker_stop = threading.Event()
        self._cam_worker_thread: threading.Thread | None = None
        self._cam_cmd_q: queue.Queue[tuple[str, object]] = queue.Queue()
        self._cam_preview_q: queue.Queue[tuple[float, str, float | None]] = queue.Queue(maxsize=1)  # (ts, b64png, sharp)
        self._cam_preview_last_ts: float | None = None
        self._cam_preview_last_sent_w: int = 960
        self._cam_connected_index: int | None = None
        self._cam_indices: list[int] = []
        self._cam_index_var = tk.StringVar()
        self._cam_status_var = tk.StringVar(value="Camera: Disconnected")
        self._cam_config = UvcCameraConfig()
        self._cam_setup_seen: set[int] = set()
        self._cam_setup_dialog: tk.Toplevel | None = None
        self._cam_af_active = False
        self._cam_af_stop = threading.Event()
        self._cam_preview_active = False
        self._cam_preview_after_id: str | None = None
        self._cam_preview_photo: tk.PhotoImage | None = None
        self._cam_preview_frame_count = 0
        self._cam_preview_consec_fail = 0  # UI-only (worker maintains its own count)
        self._cam_preview_last_fail_note_ts = 0.0  # UI-only
        self._cam_preview_sharp_var = tk.StringVar(value="Sharpness: ?")
        self._cam_preview_info_var = tk.StringVar(value="")
        self._cam_sharp_hist: deque[tuple[float, float]] = deque(maxlen=240)
        self._cam_sharp_plot_last_draw: float = 0.0
        self._cam_frame_cond = threading.Condition()
        self._cam_frame_seq = 0
        self._cam_latest_sharpness: float | None = None
        self._cam_latest_frame_size: tuple[int, int] | None = None  # (w, h) from raw frame
        self._cam_latest_cap_info: str = ""
        self._cam_latest_readback: dict[str, float] = {}
        self._cam_sharp_samples: deque[tuple[float, float]] = deque(maxlen=4000)
        self._cam_latest_frame: object | None = None  # last raw frame (numpy array), best-effort
        self._cam_worker_fail_count: int = 0

        # Bed scan (camera mosaic) state.
        self._scan_active = False
        self._scan_stop = threading.Event()

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
        self._confirm_motion_var = tk.BooleanVar(value=False)
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

        # Scan tab (camera tile scan) UI state.
        self._scan_status_var = tk.StringVar(value="Scan: Idle")
        self._scan_estimate_var = tk.StringVar(value="Estimate: —")
        self._scan_stitch_progress_var = tk.DoubleVar(value=0.0)
        self._scan_stitch_progress_text_var = tk.StringVar(value="Stitching: idle")
        self._scan_x_min_var = tk.DoubleVar(value=float(self._bed_x_min_var.get()))
        self._scan_x_max_var = tk.DoubleVar(value=float(self._bed_x_max_var.get()))
        self._scan_y_min_var = tk.DoubleVar(value=float(self._bed_y_min_var.get()))
        self._scan_y_max_var = tk.DoubleVar(value=float(self._bed_y_max_var.get()))
        self._scan_step_x_var = tk.StringVar(value="1")
        self._scan_step_y_var = tk.StringVar(value="2")
        self._scan_serpentine_var = tk.BooleanVar(value=True)
        self._scan_focus_plane_var = tk.BooleanVar(value=False)
        self._scan_focus_mesh_var = tk.StringVar(value="3x3")
        self._scan_af_each_tile_var = tk.BooleanVar(value=True)
        self._scan_shots_var = tk.StringVar(value="1")
        self._scan_stack_var = tk.StringVar(value="none")
        self._scan_capture_settle_ms_var = tk.StringVar(value="10")
        self._scan_build_pyramid_var = tk.BooleanVar(value=True)
        self._scan_build_deepzoom_var = tk.BooleanVar(value=False)
        self._scan_tiff_compression_var = tk.StringVar(value="lzw")
        # DeepZoom pyramid output.
        self._scan_deepzoom_tile_var = tk.StringVar(value="512")
        self._scan_deepzoom_format_var = tk.StringVar(value="jpg")
        self._scan_deepzoom_jpeg_quality_var = tk.StringVar(value="80")
        self._scan_out_dir_var = tk.StringVar(value=os.path.join(os.getcwd(), "scans"))

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
        self._kb_enforce_bounds = True
        self._kb_speed_xy_cap: float | None = None
        self._kb_speed_z_cap: float | None = None

        # Internal binding/paint bookkeeping for realtime modes.
        self._kb_bind_press_id: str | None = None
        self._kb_bind_release_id: str | None = None
        self._kb_bind_widget: tk.Misc | None = None
        self._rt_redraw_after_id: str | None = None
        self._rt_last_redraw_time: float | None = None

        self._build_ui()
        self._set_controls_connected(False)
        self._set_camera_controls_connected(False)
        self.refresh_ports()
        # Startup: do not probe cameras (slow and may trigger permission prompts).
        # Users can type an index manually or click Scan.
        try:
            self.cam_combo["values"] = [str(i) for i in range(6)]
        except Exception:
            pass
        if not self._cam_index_var.get().strip():
            self._cam_index_var.set("0")
        self._cam_status_var.set("Camera: Disconnected (enter index or click Scan)")

        self.after(50, self._drain_events)
        self.after(500, self._poll_tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        cam_top = ttk.Frame(self, padding=10)
        cam_top.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(cam_top, text="Camera:").grid(row=0, column=0, sticky=tk.W)
        self.cam_combo = ttk.Combobox(cam_top, textvariable=self._cam_index_var, width=10, state="normal")
        self.cam_combo.grid(row=0, column=1, sticky=tk.W, padx=(6, 10))

        self.cam_refresh_btn = ttk.Button(cam_top, text="Scan", command=self.refresh_cameras)
        self.cam_refresh_btn.grid(row=0, column=2, sticky=tk.W)

        self.cam_connect_btn = ttk.Button(cam_top, text="Connect", command=self.toggle_camera_connect)
        self.cam_connect_btn.grid(row=0, column=3, sticky=tk.W, padx=(10, 0))

        self.cam_setup_btn = ttk.Button(cam_top, text="Setup", command=self.open_camera_setup)
        self.cam_setup_btn.grid(row=0, column=4, sticky=tk.W, padx=(10, 0))

        self.cam_preview_btn = ttk.Button(cam_top, text="Preview", command=self.toggle_camera_preview)
        self.cam_preview_btn.grid(row=0, column=5, sticky=tk.W, padx=(10, 0))

        self.cam_af_btn = ttk.Button(cam_top, text="Auto Focus (Z)", command=self.toggle_camera_autofocus)
        self.cam_af_btn.grid(row=0, column=6, sticky=tk.W, padx=(10, 0))

        ttk.Label(cam_top, textvariable=self._cam_status_var).grid(
            row=1, column=0, columnspan=7, sticky=tk.W, pady=(8, 0)
        )

        for col in range(7):
            cam_top.grid_columnconfigure(col, weight=0)
        cam_top.grid_columnconfigure(1, weight=1)

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
        scan_tab = ttk.Frame(self.notebook, padding=10)
        temps_tab = ttk.Frame(self.notebook, padding=10)
        tuning_tab = ttk.Frame(self.notebook, padding=10)
        maint_tab = ttk.Frame(self.notebook, padding=10)

        self.notebook.add(quick_tab, text="Quick")
        self.notebook.add(move_tab, text="Move")
        self.notebook.add(bed_tab, text="Bed")
        self.notebook.add(bed_realtime_tab, text="Bed Realtime")
        self.notebook.add(scan_tab, text="Scan")
        self.notebook.add(temps_tab, text="Temps/Fan")
        self.notebook.add(tuning_tab, text="Tuning")
        self.notebook.add(maint_tab, text="Level/EEPROM")

        self._build_quick_tab(quick_tab)
        self._build_move_tab(move_tab)
        self._build_bed_tab(bed_tab)
        self._build_bed_realtime_tab(bed_realtime_tab)
        self._build_scan_tab(scan_tab)
        self._build_temps_tab(temps_tab)
        self._build_tuning_tab(tuning_tab)
        self._build_maint_tab(maint_tab)

        self.cam_preview_frame = ttk.LabelFrame(console, text="Camera Preview", padding=6)
        self.cam_preview_frame.configure(height=340)
        self.cam_preview_toolbar = ttk.Frame(self.cam_preview_frame)
        self.cam_preview_toolbar.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(self.cam_preview_toolbar, textvariable=self._cam_preview_info_var).pack(side=tk.LEFT)
        ttk.Label(self.cam_preview_toolbar, textvariable=self._cam_preview_sharp_var).pack(side=tk.LEFT, padx=(12, 0))
        self.cam_preview_stop_btn = ttk.Button(self.cam_preview_toolbar, text="Stop Preview", command=self.stop_camera_preview)
        self.cam_preview_stop_btn.pack(side=tk.RIGHT)

        self.cam_sharp_canvas = tk.Canvas(
            self.cam_preview_frame,
            height=60,
            bg="#111111",
            highlightthickness=1,
            highlightbackground="#333333",
        )
        self.cam_sharp_canvas.pack(side=tk.TOP, fill=tk.X, expand=False, pady=(6, 0))

        self.cam_preview_label = tk.Label(
            self.cam_preview_frame,
            text="Preview stopped",
            bg="black",
            fg="white",
        )
        self.cam_preview_label.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(6, 0))

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
            if self._scan_active:
                # Avoid injecting M105/M114 while scan motion/capture is active.
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
        with self._job_waiters_lock:
            ev = self._job_waiters.get(job.tag)
            if ev is not None:
                self._job_waiter_results[job.tag] = (bool(ok), list(lines))
                ev.set()

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

    def _next_waiter_tag(self, prefix: str) -> str:
        prefix = prefix.strip() or "wait"
        with self._job_waiters_lock:
            self._job_waiter_seq += 1
            return f"{prefix}:{self._job_waiter_seq}"

    def _send_and_wait(
        self,
        cmd: str,
        *,
        timeout_s: float,
        tag_prefix: str,
        log: bool = False,
    ) -> tuple[bool, list[str]]:
        if self._worker is None:
            return (False, [])

        tag = self._next_waiter_tag(tag_prefix)
        ev = threading.Event()
        with self._job_waiters_lock:
            self._job_waiters[tag] = ev

        try:
            if not self._send(cmd, log=log, tag=tag, timeout_s=timeout_s, interactive=False):
                return (False, [])

            if not ev.wait(float(timeout_s) + 5.0):
                return (False, [])

            with self._job_waiters_lock:
                result = self._job_waiter_results.get(tag)
            if result is None:
                return (False, [])
            return result
        finally:
            with self._job_waiters_lock:
                self._job_waiters.pop(tag, None)
                self._job_waiter_results.pop(tag, None)

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

    def refresh_cameras(self) -> None:
        if self._cam_connected or self._cam_connecting:
            return
        if self._cam_scanning:
            return

        try:
            import cv2  # type: ignore  # noqa: F401
        except Exception:
            self._cam_indices = []
            try:
                self.cam_combo["values"] = []
            except Exception:
                pass
            self._cam_index_var.set("")
            if not self._cam_connected:
                self._cam_status_var.set("Camera: OpenCV not installed (pip install opencv-python).")
            return

        self._cam_scanning = True
        self._cam_status_var.set("Camera: Scanning… (may trigger macOS camera prompts)")
        self._set_camera_controls_connected(False)
        threading.Thread(target=self._camera_scan_worker, daemon=True).start()

    def _camera_scan_worker(self) -> None:
        # Note: probing by index requires opening devices (can be slow and may trigger prompts).
        try:
            probes = probe_uvc_indices(max_index=6, read_tries=1)
            payload = [(int(p.index), bool(p.opened), bool(p.frame_ok), str(p.info)) for p in probes]
            self._events.put(("cam-scan-done", payload))
        except Exception as exc:
            self._events.put(("cam-scan-failed", str(exc)))

    def _resolve_camera_index(self) -> int | None:
        text = self._cam_index_var.get().strip()
        if not text:
            return None
        # Accept either a bare integer ("1") or a descriptive label ("1 - 1280x720 ...").
        num = ""
        for ch in text:
            if ch.isdigit() or (ch == "-" and not num):
                num += ch
                continue
            if num:
                break
        try:
            return int(num) if num else None
        except Exception:
            return None

    def _ensure_camera_worker(self) -> None:
        t = self._cam_worker_thread
        if t is not None and t.is_alive():
            return
        self._cam_worker_stop.clear()
        self._cam_worker_thread = threading.Thread(target=self._camera_worker_loop, daemon=True)
        self._cam_worker_thread.start()

    def _camera_cfg_snapshot(self) -> UvcCameraConfig:
        # `UvcCameraConfig` contains only primitives; `replace` is a cheap safe copy.
        return replace(self._cam_config)

    def _cam_send_cmd(self, cmd: str, payload: object = None) -> None:
        self._ensure_camera_worker()
        try:
            self._cam_cmd_q.put_nowait((str(cmd), payload))
        except Exception:
            pass

    def _camera_worker_loop(self) -> None:
        cap = None
        idx: int | None = None
        preview_enabled = False
        cfg = self._camera_cfg_snapshot()

        last_ui_push = 0.0
        last_info = 0.0
        last_readback = 0.0
        last_auto_restart = 0.0
        display_max_w = 960
        fail_count = 0
        enc_cond = threading.Condition()
        enc_job: tuple[float, object, float | None, UvcCameraConfig, int] | None = None
        enc_stop = threading.Event()

        try:
            import cv2  # type: ignore
        except Exception:
            cv2 = None  # type: ignore[assignment]

        def _close_cap() -> None:
            nonlocal cap, idx
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
            cap = None
            idx = None

        def _open_cap(open_idx: int) -> tuple[bool, str, bool]:
            nonlocal cap, idx
            if cv2 is None:
                return (False, "OpenCV not installed", False)
            _close_cap()

            tried_avf = False
            try:
                cap = cv2.VideoCapture(int(open_idx))
                if (not cap.isOpened()) and hasattr(cv2, "CAP_AVFOUNDATION"):
                    try:
                        cap.release()
                    except Exception:
                        pass
                    cap = cv2.VideoCapture(int(open_idx), cv2.CAP_AVFOUNDATION)
                    tried_avf = True
                if not cap.isOpened():
                    _close_cap()
                    return (False, "Failed to open camera", False)
            except Exception as exc:
                _close_cap()
                return (False, str(exc), False)

            def _try_read_frames(c) -> bool:
                try:
                    for _i in range(3):
                        ok, frame = c.read()
                        if ok and frame is not None:
                            return True
                        time.sleep(0.05)
                except Exception:
                    return False
                return False

            try:
                # Reduce latency where supported (backend-specific).
                if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1.0)
            except Exception:
                pass

            # Apply stream + control settings before reading frames.
            try:
                apply_uvc_config(cap, cfg)
            except Exception:
                pass

            frame_ok = _try_read_frames(cap)
            if (not frame_ok) and (not tried_avf) and hasattr(cv2, "CAP_AVFOUNDATION"):
                try:
                    cap.release()
                except Exception:
                    pass
                try:
                    cap2 = cv2.VideoCapture(int(open_idx), cv2.CAP_AVFOUNDATION)
                    if cap2.isOpened():
                        cap = cap2
                        try:
                            apply_uvc_config(cap, cfg)
                        except Exception:
                            pass
                        frame_ok = _try_read_frames(cap)
                except Exception:
                    pass

            info = "?"
            try:
                info = get_capture_info(cap)
            except Exception:
                pass

            idx = int(open_idx)
            return (True, info, bool(frame_ok))

        def _encoder_loop() -> None:
            nonlocal enc_job
            while not self._cam_worker_stop.is_set() and (not enc_stop.is_set()):
                job = None
                with enc_cond:
                    if enc_job is None:
                        enc_cond.wait(timeout=0.25)
                    job = enc_job
                    enc_job = None
                if job is None:
                    continue
                if cv2 is None:
                    continue
                ts, fr, sharp, cfg_job, disp_w = job
                try:
                    disp = transform_frame(
                        fr,
                        rotation_deg=cfg_job.rotation_deg,
                        crop_left_pct=cfg_job.crop_left_pct,
                        crop_top_pct=cfg_job.crop_top_pct,
                        crop_right_pct=cfg_job.crop_right_pct,
                        crop_bottom_pct=cfg_job.crop_bottom_pct,
                        max_width=int(disp_w),
                    )
                except Exception:
                    disp = fr
                try:
                    if len(disp.shape) == 2:  # type: ignore[attr-defined]
                        rgb = cv2.cvtColor(disp, cv2.COLOR_GRAY2RGB)
                    elif disp.shape[2] == 4:  # type: ignore[index]
                        rgb = cv2.cvtColor(disp, cv2.COLOR_BGRA2RGB)
                    else:
                        rgb = cv2.cvtColor(disp, cv2.COLOR_BGR2RGB)
                    ok2, buf = cv2.imencode(".png", rgb)
                    if ok2:
                        b64 = base64.b64encode(buf.tobytes()).decode("ascii")
                        try:
                            self._cam_preview_q.put_nowait((float(ts), b64, sharp))
                        except queue.Full:
                            try:
                                _ = self._cam_preview_q.get_nowait()
                            except queue.Empty:
                                pass
                            try:
                                self._cam_preview_q.put_nowait((float(ts), b64, sharp))
                            except Exception:
                                pass
                except Exception:
                    pass

        enc_thread = threading.Thread(target=_encoder_loop, daemon=True)
        enc_thread.start()

        while not self._cam_worker_stop.is_set():
            # Commands first (best-effort)
            try:
                cmd, payload = self._cam_cmd_q.get(timeout=0.01 if (preview_enabled and cap is not None) else 0.25)
            except queue.Empty:
                cmd, payload = ("", None)

            if cmd:
                cmd_s = str(cmd)
                if cmd_s == "open":
                    if isinstance(payload, tuple) and len(payload) == 2:
                        try:
                            open_idx_p, cfg_p = payload  # type: ignore[misc]
                        except Exception:
                            open_idx_p, cfg_p = payload, None
                        if isinstance(cfg_p, UvcCameraConfig):
                            cfg = cfg_p
                        payload = open_idx_p
                    try:
                        open_idx = int(payload)  # type: ignore[arg-type]
                    except Exception:
                        self._events.put(("cam-open-failed", ("?", "Invalid camera index.")))
                        continue
                    ok, info, frame_ok = _open_cap(open_idx)
                    if ok:
                        suffix = "" if frame_ok else " (no frames)"
                        self._events.put(("cam-opened", (open_idx, f"{info}{suffix}")))
                        fail_count = 0
                    else:
                        self._events.put(("cam-open-failed", (open_idx, info)))
                    continue

                if cmd_s == "close":
                    _close_cap()
                    preview_enabled = False
                    fail_count = 0
                    with self._cam_frame_cond:
                        self._cam_latest_frame = None
                        self._cam_worker_fail_count = 0
                        self._cam_frame_cond.notify_all()
                    self._events.put(("cam-closed", None))
                    continue

                if cmd_s == "set_preview":
                    preview_enabled = bool(payload)
                    continue

                if cmd_s == "set_display_w":
                    try:
                        display_max_w = max(240, int(payload))  # type: ignore[arg-type]
                    except Exception:
                        pass
                    continue

                if cmd_s == "apply_cfg":
                    try:
                        if isinstance(payload, UvcCameraConfig):
                            cfg = payload
                        else:
                            cfg = self._camera_cfg_snapshot()
                    except Exception:
                        cfg = self._camera_cfg_snapshot()
                    try:
                        if cap is not None:
                            apply_uvc_config(cap, cfg)
                    except Exception:
                        pass
                    continue

                if cmd_s == "restart":
                    # payload can be an index; reuse current index if None.
                    open_idx = idx
                    try:
                        if isinstance(payload, tuple) and len(payload) == 2:
                            open_idx_p, cfg_p = payload  # type: ignore[misc]
                            if isinstance(cfg_p, UvcCameraConfig):
                                cfg = cfg_p
                            payload = open_idx_p
                        if payload is not None:
                            open_idx = int(payload)  # type: ignore[arg-type]
                    except Exception:
                        pass
                    if open_idx is None:
                        self._events.put(("cam-open-failed", ("?", "Camera not connected.")))
                        continue
                    ok, info, frame_ok = _open_cap(int(open_idx))
                    if ok:
                        suffix = "" if frame_ok else " (no frames)"
                        self._events.put(("cam-opened", (int(open_idx), f"{info}{suffix}")))
                        fail_count = 0
                    else:
                        self._events.put(("cam-open-failed", (int(open_idx), info)))
                    continue

            if not preview_enabled or cap is None:
                continue

            # Capture a frame (may block, but we're in a worker thread).
            ok = False
            frame = None
            try:
                ok, frame = cap.read()
            except Exception:
                ok, frame = False, None

            now = time.monotonic()
            if (not ok) or (frame is None):
                # Quick retry can smooth transient backend hiccups.
                try:
                    ok, frame = cap.read()
                except Exception:
                    ok, frame = False, None
                if (not ok) or (frame is None):
                    fail_count += 1
                    with self._cam_frame_cond:
                        self._cam_worker_fail_count = int(fail_count)
                        self._cam_frame_cond.notify_all()
                    if fail_count in {1, 10, 30, 60}:
                        self._events.put(("cam-preview-fail", int(fail_count)))
                    # Best-effort auto-recover if the backend gets wedged.
                    if idx is not None and fail_count >= 90 and (now - last_auto_restart) >= 5.0:
                        last_auto_restart = now
                        try:
                            self._events.put(("cam-status", "Camera: Restarting stream (read failures)…"))
                        except Exception:
                            pass
                        try:
                            ok2, info2, frame_ok2 = _open_cap(int(idx))
                        except Exception:
                            ok2, info2, frame_ok2 = False, "restart failed", False
                        if ok2:
                            suffix = "" if frame_ok2 else " (no frames)"
                            try:
                                self._events.put(("cam-status", f"Camera: Stream restarted ({info2}{suffix})"))
                            except Exception:
                                pass
                            fail_count = 0
                            with self._cam_frame_cond:
                                self._cam_worker_fail_count = 0
                                self._cam_frame_cond.notify_all()
                    time.sleep(0.01)
                    continue
            fail_count = 0

            raw_w = 0
            raw_h = 0
            try:
                raw_h, raw_w = frame.shape[:2]
            except Exception:
                pass

            # Compute sharpness on the raw frame (software transforms applied inside the function).
            sharp: float | None = None
            try:
                sharp = compute_sharpness(
                    frame,
                    rotation_deg=cfg.rotation_deg,
                    crop_left_pct=cfg.crop_left_pct,
                    crop_top_pct=cfg.crop_top_pct,
                    crop_right_pct=cfg.crop_right_pct,
                    crop_bottom_pct=cfg.crop_bottom_pct,
                )
            except Exception:
                sharp = None

            cap_info = ""
            if (now - last_info) >= 0.5:
                last_info = now
                try:
                    cap_info = get_capture_info(cap)
                except Exception:
                    cap_info = ""

            rb: dict[str, float] = {}
            if cv2 is not None and (now - last_readback) >= 1.0:
                last_readback = now

                def _get(prop: int) -> float | None:
                    try:
                        v = float(cap.get(prop))
                    except Exception:
                        return None
                    if v != v:
                        return None
                    return v

                for name, prop in (
                    ("focus", cv2.CAP_PROP_FOCUS),
                    ("exposure", cv2.CAP_PROP_EXPOSURE),
                    ("wb_temp", cv2.CAP_PROP_WB_TEMPERATURE),
                    ("brightness", cv2.CAP_PROP_BRIGHTNESS),
                    ("contrast", cv2.CAP_PROP_CONTRAST),
                    ("saturation", cv2.CAP_PROP_SATURATION),
                    ("hue", cv2.CAP_PROP_HUE),
                    ("gamma", cv2.CAP_PROP_GAMMA),
                    ("gain", cv2.CAP_PROP_GAIN),
                ):
                    v = _get(prop)
                    if v is not None:
                        rb[name] = float(v)

                sh_prop = getattr(cv2, "CAP_PROP_SHARPNESS", None)
                if sh_prop is not None:
                    v = _get(int(sh_prop))
                    if v is not None:
                        rb["sharpness"] = float(v)

            with self._cam_frame_cond:
                self._cam_frame_seq += 1
                self._cam_latest_sharpness = sharp
                self._cam_latest_frame_size = (raw_w, raw_h) if (raw_w and raw_h) else None
                self._cam_latest_frame = frame
                self._cam_worker_fail_count = 0
                if cap_info:
                    self._cam_latest_cap_info = cap_info
                if sharp is not None:
                    self._cam_sharp_samples.append((now, float(sharp)))
                if rb:
                    self._cam_latest_readback = rb
                self._cam_frame_cond.notify_all()

            # Push a display frame at a lower rate to keep the main thread snappy.
            ui_interval = 0.1
            if getattr(self, "_rt_active", False) or getattr(self, "_kb_active", False):
                ui_interval = 0.2
            if getattr(self, "_scan_active", False):
                ui_interval = max(ui_interval, 0.2)
            if (now - last_ui_push) < float(ui_interval):
                continue
            last_ui_push = now
            # Offload PNG encoding to a separate thread so sharpness sampling stays fast.
            with enc_cond:
                enc_job = (float(now), frame, sharp, cfg, int(display_max_w))
                enc_cond.notify_all()
        _close_cap()
        enc_stop.set()
        with enc_cond:
            enc_cond.notify_all()
        self._events.put(("cam-closed", None))
    def toggle_camera_connect(self) -> None:
        if self._cam_connected or self._cam_connecting:
            self.disconnect_camera()
        else:
            self.connect_camera()

    def connect_camera(self) -> None:
        if self._cam_connected or self._cam_connecting:
            return

        idx = self._resolve_camera_index()
        if idx is None:
            messagebox.showerror("Camera", "Please select a camera index.")
            return

        try:
            import cv2  # type: ignore  # noqa: F401
        except Exception:
            messagebox.showerror(
                "Camera",
                "OpenCV not installed.\n\nInstall with:\npython -m pip install opencv-python",
            )
            return

        self._cam_connecting = True
        self._cam_status_var.set(f"Camera: Connecting (index {idx})…")
        self._set_camera_controls_connected(False)
        self._cam_send_cmd("open", (idx, self._camera_cfg_snapshot()))

    def restart_camera_stream(self, *, interactive: bool = True) -> bool:
        if self._cam_af_active:
            if interactive:
                messagebox.showerror("Camera", "Stop Auto Focus before restarting the camera stream.")
            return False
        if not self._cam_connected:
            if interactive:
                messagebox.showerror("Camera", "Camera not connected.")
            return False

        idx = self._resolve_camera_index()
        if idx is None:
            if interactive:
                messagebox.showerror("Camera", "Please select a camera index.")
            return False

        try:
            import cv2  # type: ignore  # noqa: F401
        except Exception:
            if interactive:
                messagebox.showerror(
                    "Camera",
                    "OpenCV not installed.\n\nInstall with:\npython -m pip install opencv-python",
                )
            return False

        self._cam_connecting = True
        self._cam_status_var.set(f"Camera: Restarting stream (index {idx})…")
        self._set_camera_controls_connected(False)
        self._cam_send_cmd("restart", (idx, self._camera_cfg_snapshot()))
        return True

    def disconnect_camera(self, *, force: bool = False) -> None:
        if self._cam_af_active and (not force):
            messagebox.showerror("Camera", "Stop Auto Focus before disconnecting the camera.")
            return

        # Ensure any in-flight AF thread exits promptly (best-effort).
        try:
            self._cam_af_stop.set()
        except Exception:
            pass
        self._cam_af_active = False

        self.stop_camera_preview(force=force)

        if self._cam_setup_dialog is not None:
            try:
                self._cam_setup_dialog.destroy()
            except Exception:
                pass
            self._cam_setup_dialog = None
        self._cam_connected = False
        self._cam_connecting = False
        self._cam_connected_index = None
        self._cam_status_var.set("Camera: Disconnected")
        self._set_camera_controls_connected(False)
        self._cam_send_cmd("close", None)

    def open_camera_setup(self) -> None:
        if not self._cam_connected:
            messagebox.showerror("Camera Setup", "Camera not connected.")
            return
        if self._cam_setup_dialog is not None:
            try:
                self._cam_setup_dialog.lift()
                self._cam_setup_dialog.focus_force()
            except Exception:
                pass
            return

        if not self._cam_preview_active:
            # Keep the preview running so changes are visible in real-time.
            self.start_camera_preview()

        dlg = tk.Toplevel(self)
        self._cam_setup_dialog = dlg
        dlg.title("Camera Setup (UVC)")
        dlg.transient(self)
        dlg.resizable(False, False)

        outer = ttk.Frame(dlg, padding=10)
        outer.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        note = ttk.Label(
            outer,
            text="Note: format (FourCC) and exposure/WB/focus controls are backend-specific; some changes may require reconnect.",
        )
        note.pack(side=tk.TOP, anchor=tk.W, pady=(0, 10))

        stream = ttk.LabelFrame(outer, text="Stream", padding=10)
        stream.pack(side=tk.TOP, fill=tk.X)

        try:
            import cv2  # type: ignore
        except Exception:
            cv2 = None  # type: ignore[assignment]

        with self._cam_frame_cond:
            last_size = self._cam_latest_frame_size
        res_init = f"{last_size[0]}x{last_size[1]}" if last_size else f"{self._cam_config.width}x{self._cam_config.height}"

        res_var = tk.StringVar(value=res_init)
        fps_var = tk.StringVar(value=str(self._cam_config.fps))
        fmt_var = tk.StringVar(value="Auto" if self._cam_config.fourcc is None else str(self._cam_config.fourcc))
        rot_var = tk.StringVar(value=str(int(self._cam_config.rotation_deg)))

        res_values = [
            "3840x2160 (4K UHD)",
            "4096x2160 (DCI 4K)",
            "2560x1440 (1440p)",
            "1920x1200",
            "1920x1080 (1080p)",
            "1280x720 (720p)",
            "640x480 (VGA)",
        ]

        ttk.Label(stream, text="Resolution:").grid(row=0, column=0, sticky=tk.W)
        res_combo = ttk.Combobox(stream, textvariable=res_var, values=res_values, width=18, state="normal")
        res_combo.grid(row=0, column=1, sticky=tk.W, padx=(6, 16))

        ttk.Label(stream, text="FPS:").grid(row=0, column=2, sticky=tk.W)
        fps_combo = ttk.Combobox(
            stream,
            textvariable=fps_var,
            values=["5", "10", "15", "24", "30", "50", "60"],
            width=6,
            state="normal",
        )
        fps_combo.grid(row=0, column=3, sticky=tk.W, padx=(6, 16))

        ttk.Label(stream, text="Format:").grid(row=0, column=4, sticky=tk.W)
        fmt_combo = ttk.Combobox(
            stream,
            textvariable=fmt_var,
            values=["Auto", "MJPG", "YUY2", "NV12", "UYVY", "H264"],
            width=8,
            state="normal",
        )
        fmt_combo.grid(row=0, column=5, sticky=tk.W, padx=(6, 0))

        ttk.Label(stream, text="Rotation:").grid(row=1, column=0, sticky=tk.W, pady=(8, 0))
        rot_combo = ttk.Combobox(
            stream,
            textvariable=rot_var,
            values=["0", "90", "180", "270"],
            width=6,
            state="readonly",
        )
        rot_combo.grid(row=1, column=1, sticky=tk.W, padx=(6, 16), pady=(8, 0))

        crop = ttk.LabelFrame(outer, text="Crop (software, %)", padding=10)
        crop.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))

        c_l_var = tk.DoubleVar(value=float(self._cam_config.crop_left_pct))
        c_t_var = tk.DoubleVar(value=float(self._cam_config.crop_top_pct))
        c_r_var = tk.DoubleVar(value=float(self._cam_config.crop_right_pct))
        c_b_var = tk.DoubleVar(value=float(self._cam_config.crop_bottom_pct))

        c_l_txt = tk.StringVar(value=f"{c_l_var.get():.1f}")
        c_t_txt = tk.StringVar(value=f"{c_t_var.get():.1f}")
        c_r_txt = tk.StringVar(value=f"{c_r_var.get():.1f}")
        c_b_txt = tk.StringVar(value=f"{c_b_var.get():.1f}")

        ttk.Label(crop, text="Left:").grid(row=0, column=0, sticky=tk.W)
        ttk.Scale(
            crop,
            from_=0.0,
            to=49.0,
            variable=c_l_var,
            orient=tk.HORIZONTAL,
            length=220,
            command=lambda _v: (c_l_txt.set(f"{c_l_var.get():.1f}"), _schedule_apply()),
        ).grid(row=0, column=1, sticky=tk.W, padx=(6, 10))
        ttk.Label(crop, textvariable=c_l_txt, width=6).grid(row=0, column=2, sticky=tk.W)

        ttk.Label(crop, text="Top:").grid(row=1, column=0, sticky=tk.W, pady=(8, 0))
        ttk.Scale(
            crop,
            from_=0.0,
            to=49.0,
            variable=c_t_var,
            orient=tk.HORIZONTAL,
            length=220,
            command=lambda _v: (c_t_txt.set(f"{c_t_var.get():.1f}"), _schedule_apply()),
        ).grid(row=1, column=1, sticky=tk.W, padx=(6, 10), pady=(8, 0))
        ttk.Label(crop, textvariable=c_t_txt, width=6).grid(row=1, column=2, sticky=tk.W, pady=(8, 0))

        ttk.Label(crop, text="Right:").grid(row=0, column=3, sticky=tk.W)
        ttk.Scale(
            crop,
            from_=0.0,
            to=49.0,
            variable=c_r_var,
            orient=tk.HORIZONTAL,
            length=220,
            command=lambda _v: (c_r_txt.set(f"{c_r_var.get():.1f}"), _schedule_apply()),
        ).grid(row=0, column=4, sticky=tk.W, padx=(6, 10))
        ttk.Label(crop, textvariable=c_r_txt, width=6).grid(row=0, column=5, sticky=tk.W)

        ttk.Label(crop, text="Bottom:").grid(row=1, column=3, sticky=tk.W, pady=(8, 0))
        ttk.Scale(
            crop,
            from_=0.0,
            to=49.0,
            variable=c_b_var,
            orient=tk.HORIZONTAL,
            length=220,
            command=lambda _v: (c_b_txt.set(f"{c_b_var.get():.1f}"), _schedule_apply()),
        ).grid(row=1, column=4, sticky=tk.W, padx=(6, 10), pady=(8, 0))
        ttk.Label(crop, textvariable=c_b_txt, width=6).grid(row=1, column=5, sticky=tk.W, pady=(8, 0))

        controls = ttk.LabelFrame(outer, text="Controls (best-effort)", padding=10)
        controls.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))

        lens_af_var = tk.BooleanVar(value=bool(self._cam_config.lens_autofocus))
        focus_var = tk.StringVar(value="" if self._cam_config.lens_focus is None else f"{self._cam_config.lens_focus:g}")

        auto_exp_var = tk.BooleanVar(value=bool(self._cam_config.auto_exposure))
        exp_var = tk.StringVar(value="" if self._cam_config.exposure is None else f"{self._cam_config.exposure:g}")

        auto_wb_var = tk.BooleanVar(value=bool(self._cam_config.auto_white_balance))
        wb_var = tk.StringVar(
            value="" if self._cam_config.white_balance is None else f"{self._cam_config.white_balance:g}"
        )

        prop_to_key: dict[int, str] = {}
        if cv2 is not None:
            prop_to_key = {
                int(cv2.CAP_PROP_FOCUS): "focus",
                int(cv2.CAP_PROP_EXPOSURE): "exposure",
                int(cv2.CAP_PROP_WB_TEMPERATURE): "wb_temp",
                int(cv2.CAP_PROP_BRIGHTNESS): "brightness",
                int(cv2.CAP_PROP_CONTRAST): "contrast",
                int(cv2.CAP_PROP_SATURATION): "saturation",
                int(cv2.CAP_PROP_HUE): "hue",
                int(cv2.CAP_PROP_GAMMA): "gamma",
                int(cv2.CAP_PROP_GAIN): "gain",
            }
            sh_prop = getattr(cv2, "CAP_PROP_SHARPNESS", None)
            if sh_prop is not None:
                prop_to_key[int(sh_prop)] = "sharpness"

        def _cap_get(prop: int) -> float | None:
            key = prop_to_key.get(int(prop))
            if not key:
                return None
            with self._cam_frame_cond:
                v = self._cam_latest_readback.get(key)
            if v is None:
                return None
            try:
                return float(v)
            except Exception:
                return None

        if cv2 is not None:
            if self._cam_config.lens_focus is None and not focus_var.get().strip():
                v = _cap_get(cv2.CAP_PROP_FOCUS)
                if v is not None:
                    focus_var.set(f"{v:g}")
            if self._cam_config.exposure is None and not exp_var.get().strip():
                v = _cap_get(cv2.CAP_PROP_EXPOSURE)
                if v is not None:
                    exp_var.set(f"{v:g}")
            if self._cam_config.white_balance is None and not wb_var.get().strip():
                v = _cap_get(cv2.CAP_PROP_WB_TEMPERATURE)
                if v is not None:
                    wb_var.set(f"{v:g}")

        focus_scale_var = tk.DoubleVar(value=float(focus_var.get().strip() or "0"))
        exp_scale_var = tk.DoubleVar(value=float(exp_var.get().strip() or "0"))
        wb_scale_var = tk.DoubleVar(value=float(wb_var.get().strip() or "0"))

        ttk.Checkbutton(controls, text="Lens autofocus", variable=lens_af_var).grid(row=0, column=0, sticky=tk.W)
        ttk.Label(controls, text="Focus:").grid(row=0, column=1, sticky=tk.W, padx=(16, 0))
        focus_entry = ttk.Entry(controls, textvariable=focus_var, width=10)
        focus_entry.grid(row=0, column=2, sticky=tk.W, padx=(6, 0))
        focus_scale = ttk.Scale(
            controls,
            from_=0.0,
            to=255.0,
            variable=focus_scale_var,
            orient=tk.HORIZONTAL,
            length=220,
            command=lambda v: (focus_var.set(f"{float(v):g}"), _schedule_apply()),
        )
        focus_scale.grid(row=0, column=3, sticky=tk.W, padx=(12, 0))

        ttk.Checkbutton(controls, text="Auto exposure", variable=auto_exp_var).grid(row=1, column=0, sticky=tk.W, pady=(8, 0))
        ttk.Label(controls, text="Exposure:").grid(row=1, column=1, sticky=tk.W, padx=(16, 0), pady=(8, 0))
        exp_entry = ttk.Entry(controls, textvariable=exp_var, width=10)
        exp_entry.grid(row=1, column=2, sticky=tk.W, padx=(6, 0), pady=(8, 0))
        exp_scale = ttk.Scale(
            controls,
            from_=-16.0,
            to=16.0,
            variable=exp_scale_var,
            orient=tk.HORIZONTAL,
            length=220,
            command=lambda v: (exp_var.set(f"{float(v):g}"), _schedule_apply()),
        )
        exp_scale.grid(row=1, column=3, sticky=tk.W, padx=(12, 0), pady=(8, 0))

        ttk.Checkbutton(controls, text="Auto white balance", variable=auto_wb_var).grid(
            row=2, column=0, sticky=tk.W, pady=(8, 0)
        )
        ttk.Label(controls, text="WB temp:").grid(row=2, column=1, sticky=tk.W, padx=(16, 0), pady=(8, 0))
        wb_entry = ttk.Entry(controls, textvariable=wb_var, width=10)
        wb_entry.grid(row=2, column=2, sticky=tk.W, padx=(6, 0), pady=(8, 0))
        wb_scale = ttk.Scale(
            controls,
            from_=2000.0,
            to=10000.0,
            variable=wb_scale_var,
            orient=tk.HORIZONTAL,
            length=220,
            command=lambda v: (wb_var.set(f"{float(v):.0f}"), _schedule_apply()),
        )
        wb_scale.grid(row=2, column=3, sticky=tk.W, padx=(12, 0), pady=(8, 0))

        image = ttk.LabelFrame(outer, text="Image (best-effort)", padding=10)
        image.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))

        def _img_row(
            row: int,
            *,
            label: str,
            enable_var: tk.BooleanVar,
            value_var: tk.DoubleVar,
            value_txt: tk.StringVar,
        ) -> ttk.Scale:
            ttk.Checkbutton(image, text=label, variable=enable_var).grid(row=row, column=0, sticky=tk.W)
            scale = ttk.Scale(
                image,
                from_=0.0,
                to=255.0,
                variable=value_var,
                orient=tk.HORIZONTAL,
                length=300,
                command=lambda _v: (value_txt.set(f"{value_var.get():.1f}"), _schedule_apply()),
            )
            scale.grid(row=row, column=1, sticky=tk.W, padx=(10, 10))
            ttk.Label(image, textvariable=value_txt, width=8).grid(row=row, column=2, sticky=tk.W)
            return scale

        b_en = tk.BooleanVar(value=self._cam_config.brightness is not None)
        c_en = tk.BooleanVar(value=self._cam_config.contrast is not None)
        s_en = tk.BooleanVar(value=self._cam_config.saturation is not None)
        h_en = tk.BooleanVar(value=self._cam_config.hue is not None)
        g_en = tk.BooleanVar(value=self._cam_config.gamma is not None)
        gain_en = tk.BooleanVar(value=self._cam_config.gain is not None)
        sh_en = tk.BooleanVar(value=self._cam_config.sharpness is not None)

        b_val = tk.DoubleVar(value=float(self._cam_config.brightness if self._cam_config.brightness is not None else (_cap_get(cv2.CAP_PROP_BRIGHTNESS) if cv2 else 0.0) or 0.0))
        c_val = tk.DoubleVar(value=float(self._cam_config.contrast if self._cam_config.contrast is not None else (_cap_get(cv2.CAP_PROP_CONTRAST) if cv2 else 0.0) or 0.0))
        s_val = tk.DoubleVar(value=float(self._cam_config.saturation if self._cam_config.saturation is not None else (_cap_get(cv2.CAP_PROP_SATURATION) if cv2 else 0.0) or 0.0))
        h_val = tk.DoubleVar(value=float(self._cam_config.hue if self._cam_config.hue is not None else (_cap_get(cv2.CAP_PROP_HUE) if cv2 else 0.0) or 0.0))
        g_val = tk.DoubleVar(value=float(self._cam_config.gamma if self._cam_config.gamma is not None else (_cap_get(cv2.CAP_PROP_GAMMA) if cv2 else 0.0) or 0.0))
        gain_val = tk.DoubleVar(value=float(self._cam_config.gain if self._cam_config.gain is not None else (_cap_get(cv2.CAP_PROP_GAIN) if cv2 else 0.0) or 0.0))
        sh_prop = getattr(cv2, "CAP_PROP_SHARPNESS", None) if cv2 is not None else None
        sh_val = tk.DoubleVar(value=float(self._cam_config.sharpness if self._cam_config.sharpness is not None else (_cap_get(int(sh_prop)) if sh_prop is not None else 0.0) or 0.0))

        b_txt = tk.StringVar(value=f"{b_val.get():.1f}")
        c_txt = tk.StringVar(value=f"{c_val.get():.1f}")
        s_txt = tk.StringVar(value=f"{s_val.get():.1f}")
        h_txt = tk.StringVar(value=f"{h_val.get():.1f}")
        g_txt = tk.StringVar(value=f"{g_val.get():.1f}")
        gain_txt = tk.StringVar(value=f"{gain_val.get():.1f}")
        sh_txt = tk.StringVar(value=f"{sh_val.get():.1f}")

        b_scale = _img_row(0, label="Brightness", enable_var=b_en, value_var=b_val, value_txt=b_txt)
        c_scale = _img_row(1, label="Contrast", enable_var=c_en, value_var=c_val, value_txt=c_txt)
        s_scale = _img_row(2, label="Saturation", enable_var=s_en, value_var=s_val, value_txt=s_txt)
        h_scale = _img_row(3, label="Hue", enable_var=h_en, value_var=h_val, value_txt=h_txt)
        g_scale = _img_row(4, label="Gamma", enable_var=g_en, value_var=g_val, value_txt=g_txt)
        gain_scale = _img_row(5, label="Gain", enable_var=gain_en, value_var=gain_val, value_txt=gain_txt)
        sh_scale = _img_row(6, label="Sharpness", enable_var=sh_en, value_var=sh_val, value_txt=sh_txt)

        af = ttk.LabelFrame(outer, text="Auto Focus (moves printer Z)", padding=10)
        af.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))

        af_fast_var = tk.StringVar(value=f"{self._cam_config.af_fast_step_mm:g}")
        af_slow_var = tk.StringVar(value=f"{self._cam_config.af_slow_step_mm:g}")
        af_travel_var = tk.StringVar(value=f"{self._cam_config.af_max_travel_mm:g}")
        af_settle_var = tk.StringVar(value=str(int(self._cam_config.af_settle_ms)))

        ttk.Label(af, text="Fast step (mm):").grid(row=0, column=0, sticky=tk.W)
        af_fast_entry = ttk.Entry(af, textvariable=af_fast_var, width=8)
        af_fast_entry.grid(row=0, column=1, sticky=tk.W, padx=(6, 16))
        ttk.Label(af, text="Slow step (mm):").grid(row=0, column=2, sticky=tk.W)
        af_slow_entry = ttk.Entry(af, textvariable=af_slow_var, width=8)
        af_slow_entry.grid(row=0, column=3, sticky=tk.W, padx=(6, 16))
        ttk.Label(af, text="Max travel (mm):").grid(row=0, column=4, sticky=tk.W)
        af_travel_entry = ttk.Entry(af, textvariable=af_travel_var, width=8)
        af_travel_entry.grid(row=0, column=5, sticky=tk.W, padx=(6, 0))

        ttk.Label(af, text="Settle (ms):").grid(row=1, column=0, sticky=tk.W, pady=(8, 0))
        af_settle_entry = ttk.Entry(af, textvariable=af_settle_var, width=8)
        af_settle_entry.grid(row=1, column=1, sticky=tk.W, padx=(6, 16), pady=(8, 0))

        info_var = tk.StringVar(value="")
        ttk.Label(outer, textvariable=info_var).pack(side=tk.TOP, anchor=tk.W, pady=(10, 0))

        live_var = tk.BooleanVar(value=True)
        restart_stream_var = tk.BooleanVar(value=True)
        apply_after_id: str | None = None

        def _update_entry_states() -> None:
            focus_entry.configure(state="disabled" if lens_af_var.get() else "normal")
            focus_scale.configure(state="disabled" if lens_af_var.get() else "normal")
            exp_entry.configure(state="disabled" if auto_exp_var.get() else "normal")
            exp_scale.configure(state="disabled" if auto_exp_var.get() else "normal")
            wb_entry.configure(state="disabled" if auto_wb_var.get() else "normal")
            wb_scale.configure(state="disabled" if auto_wb_var.get() else "normal")

            b_scale.configure(state="normal" if b_en.get() else "disabled")
            c_scale.configure(state="normal" if c_en.get() else "disabled")
            s_scale.configure(state="normal" if s_en.get() else "disabled")
            h_scale.configure(state="normal" if h_en.get() else "disabled")
            g_scale.configure(state="normal" if g_en.get() else "disabled")
            gain_scale.configure(state="normal" if gain_en.get() else "disabled")
            sh_scale.configure(state="normal" if sh_en.get() else "disabled")

        def _parse_resolution(text: str) -> tuple[int, int] | None:
            m = re.search(r"(\d+)\s*[xX]\s*(\d+)", text)
            if not m:
                return None
            w = int(m.group(1))
            h = int(m.group(2))
            if w <= 0 or h <= 0:
                return None
            return (w, h)

        def _apply(*, interactive: bool = True, force_restart: bool = False) -> None:
            try:
                wh = _parse_resolution(res_var.get())
                if wh is None:
                    raise ValueError("resolution")
                w, h = wh
                fps = int(float(fps_var.get().strip()))
                rot = int(rot_var.get().strip())

                c_l = float(c_l_var.get())
                c_t = float(c_t_var.get())
                c_r = float(c_r_var.get())
                c_b = float(c_b_var.get())

                af_fast = float(af_fast_var.get().strip())
                af_slow = float(af_slow_var.get().strip())
                af_travel = float(af_travel_var.get().strip())
                af_settle = int(float(af_settle_var.get().strip()))
            except Exception:
                if interactive:
                    messagebox.showerror("Camera Setup", "One or more fields are invalid.")
                return

            fmt_text = fmt_var.get().strip()
            if (not fmt_text) or (fmt_text.lower() == "auto"):
                fourcc = None
            else:
                fourcc = fmt_text.upper()
                if len(fourcc) != 4:
                    if interactive:
                        messagebox.showerror(
                            "Camera Setup",
                            "Format must be 'Auto' or a 4-character FourCC (e.g. MJPG, YUY2).",
                        )
                    return

            lens_focus: float | None = None
            exposure: float | None = None
            wb_temp: float | None = None
            try:
                if (not lens_af_var.get()) and focus_var.get().strip():
                    lens_focus = float(focus_var.get().strip())
            except Exception:
                if interactive:
                    messagebox.showerror("Camera Setup", "Invalid Focus value.")
                return
            try:
                if (not auto_exp_var.get()) and exp_var.get().strip():
                    exposure = float(exp_var.get().strip())
            except Exception:
                if interactive:
                    messagebox.showerror("Camera Setup", "Invalid Exposure value.")
                return
            try:
                if (not auto_wb_var.get()) and wb_var.get().strip():
                    wb_temp = float(wb_var.get().strip())
            except Exception:
                if interactive:
                    messagebox.showerror("Camera Setup", "Invalid WB temperature value.")
                return

            prev_stream = (
                int(self._cam_config.width),
                int(self._cam_config.height),
                int(self._cam_config.fps),
                self._cam_config.fourcc,
            )
            self._cam_config.width = max(16, w)
            self._cam_config.height = max(16, h)
            self._cam_config.fps = max(1, fps)
            self._cam_config.fourcc = fourcc
            self._cam_config.rotation_deg = rot % 360
            self._cam_config.crop_left_pct = max(0.0, min(49.0, c_l))
            self._cam_config.crop_top_pct = max(0.0, min(49.0, c_t))
            self._cam_config.crop_right_pct = max(0.0, min(49.0, c_r))
            self._cam_config.crop_bottom_pct = max(0.0, min(49.0, c_b))

            self._cam_config.lens_autofocus = bool(lens_af_var.get())
            self._cam_config.lens_focus = lens_focus
            self._cam_config.auto_exposure = bool(auto_exp_var.get())
            self._cam_config.exposure = exposure
            self._cam_config.auto_white_balance = bool(auto_wb_var.get())
            self._cam_config.white_balance = wb_temp

            self._cam_config.brightness = float(b_val.get()) if b_en.get() else None
            self._cam_config.contrast = float(c_val.get()) if c_en.get() else None
            self._cam_config.saturation = float(s_val.get()) if s_en.get() else None
            self._cam_config.hue = float(h_val.get()) if h_en.get() else None
            self._cam_config.gamma = float(g_val.get()) if g_en.get() else None
            self._cam_config.gain = float(gain_val.get()) if gain_en.get() else None
            self._cam_config.sharpness = float(sh_val.get()) if sh_en.get() else None

            self._cam_config.af_fast_step_mm = max(0.001, af_fast)
            self._cam_config.af_slow_step_mm = max(0.001, af_slow)
            self._cam_config.af_max_travel_mm = max(0.0, af_travel)
            self._cam_config.af_settle_ms = max(0, af_settle)

            stream_changed = prev_stream != (
                int(self._cam_config.width),
                int(self._cam_config.height),
                int(self._cam_config.fps),
                self._cam_config.fourcc,
            )

            did_restart = False
            if force_restart or (stream_changed and bool(restart_stream_var.get())):
                did_restart = True
                if not self.restart_camera_stream(interactive=interactive):
                    return

            if not self._cam_connected:
                if interactive:
                    messagebox.showerror("Camera Setup", "Camera disconnected.")
                return

            if not did_restart:
                self._cam_send_cmd("apply_cfg", self._camera_cfg_snapshot())
            with self._cam_frame_cond:
                info = self._cam_latest_cap_info

            idx = self._resolve_camera_index()
            idx_s = "?" if idx is None else str(idx)
            with self._cam_frame_cond:
                frame_size = self._cam_latest_frame_size
            size_s = f"{frame_size[0]}x{frame_size[1]} (frame) | " if frame_size else ""
            info_s = info if info else "?"
            self._cam_status_var.set(f"Camera: Connected (index {idx_s}, {size_s}{info_s})")
            info_var.set(f"Actual: {size_s}{info_s}")
            self._set_camera_controls_connected(True)
            _update_entry_states()

        def _schedule_apply() -> None:
            nonlocal apply_after_id
            if not bool(live_var.get()):
                return
            if apply_after_id is not None:
                try:
                    dlg.after_cancel(apply_after_id)
                except Exception:
                    pass

            def _run() -> None:
                nonlocal apply_after_id
                apply_after_id = None
                _apply(interactive=False)

            apply_after_id = dlg.after(250, _run)

        def _on_close() -> None:
            nonlocal apply_after_id
            self._cam_setup_dialog = None
            if apply_after_id is not None:
                try:
                    dlg.after_cancel(apply_after_id)
                except Exception:
                    pass
                apply_after_id = None
            try:
                dlg.destroy()
            except Exception:
                pass

        _update_entry_states()

        def _on_toggle(*_a: object) -> None:
            _update_entry_states()
            _schedule_apply()

        for _v in (
            lens_af_var,
            auto_exp_var,
            auto_wb_var,
            b_en,
            c_en,
            s_en,
            h_en,
            g_en,
            gain_en,
            sh_en,
        ):
            _v.trace_add("write", _on_toggle)

        def _bind_live(widget: tk.Misc, events: tuple[str, ...]) -> None:
            for ev in events:
                widget.bind(ev, lambda _e: _schedule_apply())

        _bind_live(res_combo, ("<<ComboboxSelected>>", "<Return>", "<FocusOut>"))
        _bind_live(fps_combo, ("<<ComboboxSelected>>", "<Return>", "<FocusOut>"))
        _bind_live(fmt_combo, ("<<ComboboxSelected>>", "<Return>", "<FocusOut>"))
        _bind_live(rot_combo, ("<<ComboboxSelected>>",))

        for _w in (
            focus_entry,
            exp_entry,
            wb_entry,
            af_fast_entry,
            af_slow_entry,
            af_travel_entry,
            af_settle_entry,
        ):
            _bind_live(_w, ("<Return>", "<FocusOut>"))

        btns = ttk.Frame(outer)
        btns.pack(side=tk.TOP, fill=tk.X, pady=(12, 0))
        ttk.Button(btns, text="Apply", command=lambda: _apply(interactive=True)).pack(side=tk.LEFT)
        ttk.Button(btns, text="Restart Stream", command=lambda: _apply(interactive=True, force_restart=True)).pack(
            side=tk.LEFT, padx=(10, 0)
        )
        ttk.Button(btns, text="Close", command=_on_close).pack(side=tk.LEFT, padx=(10, 0))

        ttk.Checkbutton(btns, text="Live apply", variable=live_var).pack(side=tk.RIGHT)
        ttk.Checkbutton(btns, text="Auto restart stream", variable=restart_stream_var).pack(
            side=tk.RIGHT, padx=(12, 0)
        )

        dlg.protocol("WM_DELETE_WINDOW", _on_close)

        def _refresh_actual() -> None:
            if self._cam_setup_dialog is not dlg:
                return
            try:
                with self._cam_frame_cond:
                    frame_size = self._cam_latest_frame_size
                    cap_info = self._cam_latest_cap_info
                size_s = f"{frame_size[0]}x{frame_size[1]} (frame) | " if frame_size else ""
                actual = "disconnected" if (not self._cam_connected) else f"{size_s}{cap_info or '?'}"
                req_fmt = "Auto" if self._cam_config.fourcc is None else str(self._cam_config.fourcc)
                requested = f"{self._cam_config.width}x{self._cam_config.height} @ {self._cam_config.fps} fps | {req_fmt}"
                mismatch = ""
                if frame_size and (
                    int(frame_size[0]) != int(self._cam_config.width) or int(frame_size[1]) != int(self._cam_config.height)
                ):
                    mismatch = " (mismatch — try Restart Stream / MJPG)"

                rb = []
                if cv2 is not None:
                    for name, prop in (
                        ("focus", cv2.CAP_PROP_FOCUS),
                        ("exp", cv2.CAP_PROP_EXPOSURE),
                        ("wb", cv2.CAP_PROP_WB_TEMPERATURE),
                        ("bri", cv2.CAP_PROP_BRIGHTNESS),
                        ("con", cv2.CAP_PROP_CONTRAST),
                        ("sat", cv2.CAP_PROP_SATURATION),
                    ):
                        v = _cap_get(prop)
                        if v is not None:
                            rb.append(f"{name}={v:g}")
                rb_line = "" if not rb else "\nReadback: " + "  ".join(rb)
                info_var.set(f"Actual: {actual}\nRequested: {requested}{mismatch}{rb_line}")
            except Exception:
                pass
            dlg.after(500, _refresh_actual)

        _refresh_actual()

    def toggle_camera_preview(self) -> None:
        if self._cam_preview_active:
            self.stop_camera_preview()
        else:
            self.start_camera_preview()

    def start_camera_preview(self) -> None:
        if not self._cam_connected:
            messagebox.showerror("Preview", "Camera not connected.")
            return

        self._cam_preview_active = True
        self._cam_preview_frame_count = 0
        self._cam_preview_consec_fail = 0
        self._cam_preview_last_fail_note_ts = 0.0
        self._cam_preview_last_ts = None
        self._cam_preview_sharp_var.set("Sharpness: ?")
        self._cam_sharp_hist.clear()
        self._cam_sharp_plot_last_draw = 0.0
        try:
            self.cam_sharp_canvas.delete("all")
        except Exception:
            pass

        try:
            with self._cam_frame_cond:
                info = self._cam_latest_cap_info
                size = self._cam_latest_frame_size
            if size and info:
                self._cam_preview_info_var.set(f"Frame: {size[0]}x{size[1]} | {info}")
            elif info:
                self._cam_preview_info_var.set(info)
            else:
                self._cam_preview_info_var.set("")
        except Exception:
            self._cam_preview_info_var.set("")

        try:
            if not self.cam_preview_frame.winfo_ismapped():
                self.cam_preview_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=False, before=self.log, pady=(0, 8))
        except Exception:
            pass

        try:
            w = int(self.cam_preview_label.winfo_width())
        except Exception:
            w = 0
        if w < 200:
            w = 900
        # Preview scales to the widget size (up to the raw camera frame width).
        max_w = max(320, w - 12)
        try:
            with self._cam_frame_cond:
                raw_size = self._cam_latest_frame_size
            if raw_size:
                max_w = min(int(max_w), int(raw_size[0]))
        except Exception:
            pass
        self._cam_preview_last_sent_w = int(max_w)
        self._cam_send_cmd("set_display_w", int(max_w))
        self._cam_send_cmd("set_preview", True)

        self._set_camera_controls_connected(True)
        self._camera_preview_tick()

    def stop_camera_preview(self, *, force: bool = False) -> None:
        if self._cam_af_active and (not force):
            messagebox.showerror("Preview", "Stop Auto Focus before stopping the preview.")
            return

        self._cam_preview_active = False
        if self._cam_preview_after_id is not None:
            try:
                self.after_cancel(self._cam_preview_after_id)
            except Exception:
                pass
            self._cam_preview_after_id = None

        self._cam_send_cmd("set_preview", False)

        self._cam_preview_photo = None
        try:
            self.cam_preview_label.configure(image="", text="Preview stopped")
        except Exception:
            pass
        self._cam_preview_consec_fail = 0
        self._cam_sharp_hist.clear()
        try:
            self.cam_sharp_canvas.delete("all")
        except Exception:
            pass
        try:
            self.cam_preview_frame.pack_forget()
        except Exception:
            pass

        self._set_camera_controls_connected(self._cam_connected)

    def _camera_preview_tick(self) -> None:
        if not self._cam_preview_active:
            return

        if not self._cam_connected:
            self.stop_camera_preview(force=True)
            return

        pkt: tuple[float, str, float | None] | None = None
        try:
            while True:
                pkt = self._cam_preview_q.get_nowait()
        except queue.Empty:
            pass

        if pkt is not None:
            ts, b64png, sharp = pkt
            self._cam_preview_last_ts = float(ts)
            self._cam_preview_frame_count += 1

            try:
                photo = tk.PhotoImage(data=b64png, format="png")
            except Exception:
                photo = None

            if photo is not None:
                self._cam_preview_photo = photo
                try:
                    self.cam_preview_label.configure(image=photo, text="")
                except Exception:
                    pass

            if sharp is not None:
                self._cam_preview_sharp_var.set(f"Sharpness: {float(sharp):.1f}")
                self._cam_sharp_hist.append((float(ts), float(sharp)))
                plot_interval = 0.1
                if self._rt_active or self._kb_active or self._scan_active:
                    plot_interval = 0.2
                if (float(ts) - float(self._cam_sharp_plot_last_draw)) >= float(plot_interval):
                    self._cam_sharp_plot_last_draw = float(ts)
                    self._camera_draw_sharpness_plot()
            else:
                self._cam_preview_sharp_var.set("Sharpness: ?")

        # Refresh the info line periodically, and show fail streak if any.
        try:
            with self._cam_frame_cond:
                size = self._cam_latest_frame_size
                info = self._cam_latest_cap_info
                fails = int(self._cam_worker_fail_count)
            size_s = "" if not size else f"Frame: {size[0]}x{size[1]} | "
            base = (size_s + info) if info else size_s.rstrip()
            if fails > 0:
                extra = f" | read failed x{fails}"
            else:
                extra = ""
            if base or extra:
                self._cam_preview_info_var.set((base or "Preview") + extra)
        except Exception:
            pass

        # Adjust display width if the UI is resized.
        try:
            w = int(self.cam_preview_label.winfo_width())
        except Exception:
            w = 0
        if w >= 200:
            max_w = max(320, w - 12)
            try:
                with self._cam_frame_cond:
                    raw_size = self._cam_latest_frame_size
                if raw_size:
                    max_w = min(int(max_w), int(raw_size[0]))
            except Exception:
                pass
            if abs(int(max_w) - int(self._cam_preview_last_sent_w)) >= 40:
                self._cam_preview_last_sent_w = int(max_w)
                self._cam_send_cmd("set_display_w", int(max_w))

        interval_ms = 50
        if self._rt_active or self._kb_active or self._scan_active:
            interval_ms = 100
        self._cam_preview_after_id = self.after(int(interval_ms), self._camera_preview_tick)

    def _camera_draw_sharpness_plot(self) -> None:
        try:
            canvas = self.cam_sharp_canvas
        except Exception:
            return

        try:
            w = int(canvas.winfo_width())
            h = int(canvas.winfo_height())
        except Exception:
            return
        if w < 80 or h < 20:
            return

        hist = list(self._cam_sharp_hist)
        canvas.delete("all")
        if len(hist) < 2:
            return

        t0 = float(hist[0][0])
        t1 = float(hist[-1][0])
        if t1 <= t0:
            t1 = t0 + 1.0

        s_vals = [float(s) for _t, s in hist]
        s_min = min(s_vals)
        s_max = max(s_vals)
        if s_max <= s_min:
            s_max = s_min + 1.0

        pad = 4
        x_span = max(1.0, float(w - 2 * pad))
        y_span = max(1.0, float(h - 2 * pad))
        dt = float(t1 - t0)
        ds = float(s_max - s_min)

        pts: list[float] = []
        for t, s in hist:
            x = pad + ((float(t) - t0) / dt) * x_span
            y = (h - pad) - ((float(s) - s_min) / ds) * y_span
            pts.extend([x, y])

        try:
            canvas.create_line(pts, fill="#00ff6a", width=2)
        except Exception:
            pass

        try:
            canvas.create_text(
                pad,
                pad,
                anchor="nw",
                fill="#dddddd",
                font=("TkDefaultFont", 8),
                text=f"{s_min:.0f}..{s_max:.0f}  now {hist[-1][1]:.1f}",
            )
            canvas.create_text(
                w - pad,
                pad,
                anchor="ne",
                fill="#999999",
                font=("TkDefaultFont", 8),
                text=f"{dt:.1f}s",
            )
        except Exception:
            pass

    def _frame_to_photoimage(self, frame) -> tk.PhotoImage | None:
        try:
            import cv2  # type: ignore
        except Exception:
            return None

        try:
            if len(frame.shape) == 2:
                rgb = cv2.cvtColor(frame, cv2.COLOR_GRAY2RGB)
            elif frame.shape[2] == 4:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGRA2RGB)
            else:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        except Exception:
            return None

        try:
            from PIL import Image, ImageTk  # type: ignore

            img = Image.fromarray(rgb)
            return ImageTk.PhotoImage(img)
        except Exception:
            pass

        try:
            ok, buf = cv2.imencode(".png", rgb)
            if ok:
                b64 = base64.b64encode(buf.tobytes()).decode("ascii")
                return tk.PhotoImage(data=b64, format="png")
        except Exception:
            pass

        h, w = rgb.shape[:2]
        header = f"P6\n{w} {h}\n255\n".encode("ascii")
        ppm = header + rgb.tobytes()
        b64 = base64.b64encode(ppm).decode("ascii")
        try:
            return tk.PhotoImage(data=b64, format="PPM")
        except Exception:
            return None

    def toggle_camera_autofocus(self) -> None:
        if self._cam_af_active:
            self._cam_af_stop.set()
            self._cam_status_var.set("Camera: Stopping AF...")
            return

        if self._scan_active:
            messagebox.showerror("Auto Focus", "Stop Scan before running Auto Focus.")
            return

        if not self._cam_connected:
            messagebox.showerror("Auto Focus", "Camera not connected.")
            return
        if self._worker is None:
            messagebox.showerror("Auto Focus", "Printer not connected (AF moves Z).")
            return
        if self._rt_active or self._kb_active:
            messagebox.showerror("Auto Focus", "Stop Realtime modes before running Auto Focus.")
            return

        if bool(self._confirm_motion_var.get()):
            ok = messagebox.askokcancel(
                "Auto Focus (Z)",
                "This will move the printer Z axis back/forth to maximize image sharpness.\n\n"
                "Make sure the nozzle/camera is clear and Z travel is safe.\n\nContinue?",
            )
            if not ok:
                return

        if not self._cam_preview_active:
            self.start_camera_preview()

        self._cam_af_active = True
        self._cam_af_stop.clear()
        self._cam_status_var.set("Camera: Auto Focus running...")
        self._set_camera_controls_connected(True)

        restore_mode = str(self._coord_mode_var.get())
        z_min = float(self._bed_z_min_var.get())
        z_max = float(self._bed_z_max_var.get())
        speed_z = float(self._speed_z_var.get())

        threading.Thread(
            target=self._camera_autofocus_thread,
            args=(restore_mode, z_min, z_max, speed_z),
            daemon=True,
        ).start()

    def _camera_autofocus_thread(
        self,
        restore_mode: str,
        z_min: float,
        z_max: float,
        speed_z_mm_s: float,
        *,
        emit_events: bool = True,
        profile: str = "full",
        start_z_hint: float | None = None,
        tile_dir_hint: int = 0,
    ) -> tuple[bool, float | None, float | None, str]:
        def status(msg: str) -> None:
            if emit_events:
                self._events.put(("cam-status", msg))

        def log(msg: str) -> None:
            if emit_events:
                self._events.put(("cam-log", msg))

        start_z: float | None = None
        current_z: float | None = None

        try:
            z_val: float | None = None
            profile_s = (profile or "full").strip().lower()
            if profile_s not in {"full", "tile"}:
                profile_s = "full"

            if start_z_hint is not None:
                try:
                    z_val = float(start_z_hint)
                    z_val = self._clamp(float(z_val), float(z_min), float(z_max))
                except Exception:
                    z_val = None

            if z_val is None:
                ok, lines = self._send_and_wait("M114", timeout_s=5.0, tag_prefix="af_m114", log=False)
                if not ok:
                    raise RuntimeError("M114 failed (printer did not respond OK).")

                for line in lines:
                    if line.lstrip().lower().startswith("count"):
                        continue
                    pos = parse_m114(line)
                    if pos is None:
                        continue
                    _x, _y, z, _e = pos
                    if z is not None:
                        z_val = float(z)
                        break
                if z_val is None:
                    raise RuntimeError("Could not parse Z from M114.")

            cfg = self._camera_cfg_snapshot()
            try:
                cam_fps = float(cfg.fps)
            except Exception:
                cam_fps = 30.0
            cam_fps = max(1.0, min(240.0, float(cam_fps)))
            # For UVC pipelines, a tiny delay / frame drop after motion helps avoid measuring
            # buffered frames captured during movement.
            min_sample_settle_s = min(0.2, 1.0 / float(cam_fps))
            drop_after_move_frames = 1
            start_z = float(z_val)
            current_z = float(z_val)

            # Ensure we have live camera frames (otherwise AF can appear to do nothing).
            with self._cam_frame_cond:
                seq = int(self._cam_frame_seq)
            seq, fr0 = self._camera_wait_for_next_frame(seq, timeout_s=3.0)
            if fr0 is None:
                status("Camera: AF waiting for camera frames…")
                try:
                    self._cam_send_cmd("set_preview", True)
                except Exception:
                    pass
                with self._cam_frame_cond:
                    seq = int(self._cam_frame_seq)
                seq, fr0 = self._camera_wait_for_next_frame(seq, timeout_s=3.0)
                if fr0 is None and self._cam_connected_index is not None:
                    status("Camera: AF restarting camera stream…")
                    try:
                        self._cam_send_cmd("restart", (int(self._cam_connected_index), self._camera_cfg_snapshot()))
                    except Exception:
                        pass
                    with self._cam_frame_cond:
                        seq = int(self._cam_frame_seq)
                    seq, fr0 = self._camera_wait_for_next_frame(seq, timeout_s=5.0)
                if fr0 is None:
                    raise RuntimeError("No camera frames. Start Preview or restart the stream.")

            def _drop_frames(seq_in: int, *, n: int, timeout_s: float) -> int:
                seq_local = int(seq_in)
                per_timeout = max(0.08, float(timeout_s) / max(1, int(n)))
                for _i in range(max(0, int(n))):
                    if self._cam_af_stop.is_set():
                        break
                    seq_local, _fr = self._camera_wait_for_next_frame(seq_local, timeout_s=per_timeout)
                return int(seq_local)

            min_frame_timeout = max(0.05, min(0.15, 2.0 / float(cam_fps)))

            def _measure_focus(seq_in: int, *, samples: int, timeout_s: float) -> tuple[int, float | None]:
                vals: list[float] = []
                seq_local = int(seq_in)
                per_timeout = max(float(min_frame_timeout), float(timeout_s) / max(1, int(samples)))
                for _i in range(max(1, int(samples))):
                    if self._cam_af_stop.is_set():
                        break
                    seq_local, frame = self._camera_wait_for_next_frame(seq_local, timeout_s=per_timeout)
                    if frame is None:
                        continue
                    try:
                        v = compute_sharpness(
                            frame,
                            rotation_deg=cfg.rotation_deg,
                            crop_left_pct=cfg.crop_left_pct,
                            crop_top_pct=cfg.crop_top_pct,
                            crop_right_pct=cfg.crop_right_pct,
                            crop_bottom_pct=cfg.crop_bottom_pct,
                            max_width=None,  # full-res metric for autofocus
                            method="tenengrad",
                        )
                        vals.append(float(v))
                    except Exception:
                        continue
                if not vals:
                    return (seq_local, None)
                vals.sort()
                return (seq_local, vals[len(vals) // 2])  # median

            start_samples = 2 if profile_s == "tile" else 3
            start_timeout = 1.2 if profile_s == "tile" else 2.5
            seq = _drop_frames(seq, n=int(drop_after_move_frames), timeout_s=0.8)
            seq, sharp0 = _measure_focus(seq, samples=int(start_samples), timeout_s=float(start_timeout))
            if sharp0 is None:
                raise RuntimeError("No focus samples from camera.")

            max_travel_cfg = max(0.0, float(cfg.af_max_travel_mm))
            slow_step = max(0.001, float(cfg.af_slow_step_mm))
            settle_s = max(0.0, float(cfg.af_settle_ms) / 1000.0)

            max_travel = float(max_travel_cfg)
            if profile_s == "tile":
                # In scans, adjacent tiles are usually close in focus; use a small local range.
                max_travel = min(float(max_travel_cfg), max(0.8, 6.0 * float(slow_step)))

            travel_min = max(float(z_min), float(start_z) - float(max_travel))
            travel_max = min(float(z_max), float(start_z) + float(max_travel))
            if travel_max <= travel_min + 1e-6:
                raise RuntimeError("AF travel range is empty (check Z bounds / max travel).")

            fast_speed = max(0.5, float(speed_z_mm_s))
            fine_speed = max(0.5, min(fast_speed, fast_speed * 0.25))
            micro_speed = max(0.5, min(fine_speed, fine_speed * 0.5))

            # Switch to relative moves once; restore in finally.
            ok_g91, _ = self._send_and_wait("G91", timeout_s=5.0, tag_prefix="af_g91", log=False)
            if not ok_g91:
                raise RuntimeError("Failed to set relative mode (G91).")

            status(f"Camera: AF start Z={start_z:.3f} focus={sharp0:.1f}")
            log(
                f"[camera] AF start: Z={start_z:.3f}, focus={sharp0:.1f} | travel=[{travel_min:.3f},{travel_max:.3f}]"
            )

            def _enqueue_waiter(cmd: str, *, timeout_s: float, tag_prefix: str) -> tuple[str, threading.Event]:
                tag = self._next_waiter_tag(tag_prefix)
                ev = threading.Event()
                with self._job_waiters_lock:
                    self._job_waiters[tag] = ev
                if not self._send(cmd, log=False, tag=tag, timeout_s=float(timeout_s), interactive=False):
                    with self._job_waiters_lock:
                        self._job_waiters.pop(tag, None)
                    raise RuntimeError(f"Failed to enqueue: {cmd}")
                return (tag, ev)

            def _wait_and_cleanup(tag: str, ev: threading.Event, *, timeout_s: float) -> bool:
                ok_local = bool(ev.wait(float(timeout_s)))
                with self._job_waiters_lock:
                    result = self._job_waiter_results.pop(tag, None)
                    self._job_waiters.pop(tag, None)
                if not ok_local or result is None:
                    return False
                return bool(result[0])

            def _move_to_z(target_z: float, speed: float, *, settle_override_s: float | None = None) -> None:
                nonlocal current_z
                if current_z is None:
                    raise RuntimeError("internal Z tracking error")
                target_z_c = self._clamp(float(target_z), float(z_min), float(z_max))
                dz = float(target_z_c) - float(current_z)
                if abs(dz) <= 1e-6:
                    return
                if not self._af_move_z_rel(dz, float(speed)):
                    raise RuntimeError("Move failed (Z).")
                current_z = float(target_z_c)
                settle_local = float(settle_s) if settle_override_s is None else max(0.0, float(settle_override_s))
                if settle_local > 1e-6:
                    time.sleep(settle_local)

            def _sweep(z0: float, z1: float, *, speed: float, sample_hz: float, label: str) -> list[tuple[float, float]]:
                nonlocal current_z, seq
                if current_z is None:
                    raise RuntimeError("internal Z tracking error")
                z0_c = self._clamp(float(z0), float(z_min), float(z_max))
                z1_c = self._clamp(float(z1), float(z_min), float(z_max))
                _move_to_z(float(z0_c), speed=fast_speed)

                dz_total = float(z1_c) - float(z0_c)
                if abs(dz_total) <= 1e-6:
                    return []
                feed = self._mm_s_to_mm_min(max(0.5, float(speed)))
                ok_g0, _ = self._send_and_wait(
                    f"G0 Z{dz_total:g} F{feed}",
                    timeout_s=5.0,
                    tag_prefix=f"af_{label}_g0",
                    log=False,
                )
                if not ok_g0:
                    raise RuntimeError(f"Move failed ({label} sweep).")

                est_dur = abs(dz_total) / max(1e-6, float(speed))
                m400_timeout = max(30.0, (est_dur * 3.0) + 10.0)
                tag_m400, ev_m400 = _enqueue_waiter("M400", timeout_s=m400_timeout, tag_prefix=f"af_{label}_m400")

                samples: list[tuple[float, float]] = []
                t0 = time.monotonic()
                interval = 1.0 / max(1.0, float(sample_hz))
                next_sample = t0
                try:
                    status(f"Camera: AF {label} sweep {dz_total:+.2f}mm @ {speed:.1f}mm/s…")
                    while not ev_m400.is_set():
                        if self._cam_af_stop.is_set():
                            break
                        now = time.monotonic()
                        if now < next_sample:
                            time.sleep(min(0.01, next_sample - now))
                            continue
                        next_sample = now + interval

                        seq, sharp = _measure_focus(seq, samples=1, timeout_s=interval)
                        if sharp is None:
                            continue
                        t1 = time.monotonic()
                        frac = (t1 - t0) / max(0.2, est_dur)
                        frac = max(0.0, min(1.0, float(frac)))
                        z_est = float(z0_c) + (frac * float(dz_total))
                        samples.append((z_est, float(sharp)))
                finally:
                    ok_m400 = _wait_and_cleanup(tag_m400, ev_m400, timeout_s=m400_timeout)
                    if not ok_m400:
                        raise RuntimeError(f"M400 failed/timeout ({label}).")

                current_z = float(z1_c)
                if settle_s:
                    time.sleep(settle_s)
                if self._cam_af_stop.is_set():
                    raise InterruptedError("stopped")
                return samples

            def _best_of(samples: list[tuple[float, float]]) -> tuple[float, float]:
                if not samples:
                    raise RuntimeError("No focus samples captured.")
                z_b, s_b = max(samples, key=lambda t: float(t[1]))
                return (float(z_b), float(s_b))

            # Stage 4: ultra refinement (fast discrete sampling).
            #
            # The sweeps estimate Z by time; this final stage samples a few exact Z positions and
            # optionally performs a tiny quadratic interpolation around the peak.
            def _refine_discrete(z_center: float, s_center: float) -> tuple[float, float]:
                nonlocal seq

                best_z2 = float(z_center)
                best_s2 = float(s_center)

                ultra_speed = max(0.5, float(micro_speed))

                # UVC pipelines can lag; enforce a small minimum settle and drop a frame after moves.
                refine_settle_cap = 0.03 if profile_s == "tile" else 0.06
                refine_settle = max(float(min_sample_settle_s), min(float(settle_s), float(refine_settle_cap)))
                drop_n = int(drop_after_move_frames)

                timeout_quick = 0.6 if profile_s == "tile" else 0.7
                samples_center = 2
                samples_quick = 1
                samples_confirm = 2 if profile_s == "tile" else 3

                if profile_s == "tile":
                    step = max(0.003, float(slow_step) / 6.0)
                    min_step = 0.001
                    rounds = 4
                else:
                    step = max(0.005, float(slow_step) / 4.0)
                    min_step = 0.002
                    rounds = 3

                cache: dict[tuple[float, int], float] = {}

                def _sample(z: float, *, samples: int, timeout_s: float) -> float | None:
                    nonlocal seq
                    zc = self._clamp(float(z), float(z_min), float(z_max))
                    zr = float(round(zc, 6))
                    key = (zr, int(samples))
                    if key in cache:
                        return float(cache[key])
                    if self._cam_af_stop.is_set():
                        raise InterruptedError("stopped")
                    _move_to_z(float(zc), speed=ultra_speed, settle_override_s=refine_settle)
                    seq = _drop_frames(seq, n=drop_n, timeout_s=max(0.25, float(timeout_s)))
                    seq, s = _measure_focus(seq, samples=int(samples), timeout_s=float(timeout_s))
                    if s is None:
                        return None
                    cache[key] = float(s)
                    return float(s)

                # Ensure we have an exact sample at the nominal center Z.
                s0 = _sample(best_z2, samples=samples_center, timeout_s=max(0.9, float(timeout_quick)))
                if s0 is not None:
                    best_s2 = float(s0)

                for _i in range(int(rounds)):
                    if float(step) < float(min_step):
                        break

                    z_m = self._clamp(float(best_z2) - float(step), float(z_min), float(z_max))
                    z_p = self._clamp(float(best_z2) + float(step), float(z_min), float(z_max))
                    if abs(float(z_p) - float(z_m)) <= 1e-9:
                        break

                    s_m = _sample(z_m, samples=samples_quick, timeout_s=float(timeout_quick))
                    s_p = _sample(z_p, samples=samples_quick, timeout_s=float(timeout_quick))

                    # Pick best of center / left / right.
                    cand: list[tuple[float, float]] = [(float(best_s2), float(best_z2))]
                    if s_m is not None:
                        cand.append((float(s_m), float(z_m)))
                    if s_p is not None:
                        cand.append((float(s_p), float(z_p)))
                    s_b, z_b = max(cand, key=lambda t: float(t[0]))

                    if abs(float(z_b) - float(best_z2)) <= 1e-9:
                        # Peak appears to be inside the bracket; try a tiny quadratic interpolation.
                        if s_m is not None and s_p is not None:
                            denom = float(s_m) - (2.0 * float(best_s2)) + float(s_p)
                            if abs(denom) > 1e-9:
                                xv = float(best_z2) + (float(step) * (float(s_m) - float(s_p))) / (2.0 * denom)
                                if float(z_min) <= float(xv) <= float(z_max):
                                    s_v = _sample(xv, samples=samples_center, timeout_s=max(0.9, float(timeout_quick)))
                                    if s_v is not None and float(s_v) > float(best_s2):
                                        best_s2 = float(s_v)
                                        best_z2 = float(round(float(xv), 6))
                        step = float(step) * 0.5
                        continue

                    # Climb toward the better side and keep the same step.
                    best_z2 = float(z_b)
                    best_s2 = float(s_b)

                # Final confirmation at chosen Z (more samples to reduce noise).
                s_f = _sample(best_z2, samples=samples_confirm, timeout_s=1.2)
                if s_f is not None:
                    best_s2 = float(s_f)
                return (float(best_z2), float(best_s2))

            # "Tile" profile: adjacent scan tiles should be close in focus.
            # Single short sweep + pick best Z (fast, robust enough for scanning tiles).
            if profile_s == "tile":
                span = max(1e-6, float(travel_max) - float(travel_min))
                sample_hz = min(24.0, max(6.0, float(cam_fps) * 0.8))
                target_samples = 12.0
                dur = max(0.25, min(0.9, float(target_samples) / max(1.0, float(sample_hz))))
                speed = max(0.5, min(float(fast_speed), float(span) / max(0.1, float(dur))))
                status(f"Camera: AF tile sweep {span:.3f}mm @ {speed:.2f}mm/s…")
                samples = _sweep(float(travel_min), float(travel_max), speed=float(speed), sample_hz=float(sample_hz), label="tile")
                z_b, s_b = _best_of(samples)
                status(f"Camera: AF tile best Z≈{z_b:.3f} focus={s_b:.1f}")
                _move_to_z(float(z_b), speed=max(0.5, float(fine_speed)))

                status(f"Camera: AF done Z≈{z_b:.3f} focus={s_b:.1f}")
                log(f"[camera] AF tile done: Z≈{z_b:.3f}, focus={s_b:.1f}")
                msg = f"AF done: Z≈{z_b:.3f} focus={s_b:.1f}"
                if emit_events:
                    self._events.put(("cam-af-finished", (True, msg)))
                return (True, float(z_b), float(s_b), msg)

            best_z = float(start_z)
            best_sharp = float(sharp0)

            # Stage 1: coarse range search (full).
            for attempt in range(2):
                samples = _sweep(travel_min, travel_max, speed=fast_speed, sample_hz=12.0, label="coarse")
                z_b, s_b = _best_of(samples)
                status(f"Camera: AF coarse best Z≈{z_b:.3f} focus={s_b:.1f}")
                if s_b > best_sharp:
                    best_z, best_sharp = float(z_b), float(s_b)
                improve = (s_b - float(sharp0)) / max(1.0, float(sharp0))
                if improve >= 0.05 or attempt == 1:
                    break
                # Expand the search range once (within hard Z bounds).
                extra = float(max_travel)
                max_travel = min(float(z_max - z_min), float(max_travel) + float(extra))
                travel_min = max(float(z_min), float(start_z) - float(max_travel))
                travel_max = min(float(z_max), float(start_z) + float(max_travel))
                status("Camera: AF expanding search range…")

            # Stage 2: fine sweep around current best.
            if profile_s == "tile":
                fine_span = max(3.0 * slow_step, 0.4)
            else:
                fine_span = max(6.0 * slow_step, 2.0)
            z0 = max(float(z_min), float(best_z) - float(fine_span))
            z1 = min(float(z_max), float(best_z) + float(fine_span))
            samples = _sweep(z0, z1, speed=fine_speed, sample_hz=18.0, label="fine")
            best_z, best_sharp = _best_of(samples)
            status(f"Camera: AF fine best Z≈{best_z:.3f} focus={best_sharp:.1f}")

            # Stage 3: micro sweep around fine best.
            if profile_s == "tile":
                micro_span = max(1.5 * slow_step, 0.25)
            else:
                micro_span = max(3.0 * slow_step, 0.8)
            z0 = max(float(z_min), float(best_z) - float(micro_span))
            z1 = min(float(z_max), float(best_z) + float(micro_span))
            samples = _sweep(z0, z1, speed=micro_speed, sample_hz=22.0, label="micro")
            best_z, best_sharp = _best_of(samples)

            best_z, best_sharp = _refine_discrete(float(best_z), float(best_sharp))
            status(f"Camera: AF refined Z≈{best_z:.3f} focus={best_sharp:.1f}")

            # Final: move to refined best estimate.
            status(f"Camera: AF final move to Z≈{best_z:.3f}…")
            _move_to_z(float(best_z), speed=micro_speed)

            status(f"Camera: AF done Z≈{best_z:.3f} focus={best_sharp:.1f}")
            log(f"[camera] AF done: Z≈{best_z:.3f}, focus={best_sharp:.1f}")
            msg = f"AF done: Z≈{best_z:.3f} focus={best_sharp:.1f}"
            if emit_events:
                self._events.put(("cam-af-finished", (True, msg)))
            return (True, float(best_z), float(best_sharp), msg)
        except InterruptedError:
            status("Camera: AF stopped.")
            log("[camera] AF stopped.")
            msg = "AF stopped."
            if emit_events:
                self._events.put(("cam-af-finished", (False, msg)))
            return (False, None, None, msg)
        except Exception as exc:
            status("Camera: AF failed.")
            log(f"[camera] AF failed: {exc}")
            msg = f"AF failed: {exc}"
            if emit_events:
                self._events.put(("cam-af-finished", (False, msg)))
            return (False, None, None, msg)
        finally:
            # Restore coordinate mode (best-effort).
            try:
                if restore_mode == "absolute":
                    self._send("G90", log=False, interactive=False)
                else:
                    self._send("G91", log=False, interactive=False)
            except Exception:
                pass
            try:
                # Refresh position after autofocus.
                if emit_events and self._worker is not None:
                    self._send("M114", log=False, priority="low", tag="poll_m114", timeout_s=3.0, interactive=False)
                    self._poll_pending_m114 = True
            except Exception:
                pass
        return (False, None, None, "AF finished.")

    def _af_move_z(self, dz_mm: float, speed_mm_s: float) -> bool:
        if self._worker is None:
            return False
        feed = self._mm_s_to_mm_min(max(0.5, float(speed_mm_s)))
        if not self._send("G91", log=False, interactive=False):
            return False
        if not self._send(f"G0 Z{dz_mm:g} F{feed}", log=False, timeout_s=10.0, interactive=False):
            return False
        ok, _lines = self._send_and_wait("M400", timeout_s=120.0, tag_prefix="af_m400", log=False)
        return bool(ok)

    def _af_move_z_rel(self, dz_mm: float, speed_mm_s: float) -> bool:
        """Relative Z move + wait for motion complete (assumes G91 already set)."""
        if self._worker is None:
            return False
        feed = self._mm_s_to_mm_min(max(0.5, float(speed_mm_s)))
        if not self._send(f"G0 Z{dz_mm:g} F{feed}", log=False, timeout_s=10.0, interactive=False):
            return False
        ok, _lines = self._send_and_wait("M400", timeout_s=120.0, tag_prefix="af_m400", log=False)
        return bool(ok)

    def _camera_get_latest_sharpness(self) -> tuple[int, float | None]:
        with self._cam_frame_cond:
            return self._cam_frame_seq, self._cam_latest_sharpness

    def _camera_wait_for_next_sharpness(self, last_seq: int, *, timeout_s: float) -> tuple[int, float | None]:
        deadline = time.monotonic() + max(0.01, float(timeout_s))
        with self._cam_frame_cond:
            while self._cam_frame_seq <= last_seq and (not self._cam_af_stop.is_set()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cam_frame_cond.wait(timeout=remaining)
            return self._cam_frame_seq, self._cam_latest_sharpness

    def _camera_wait_for_next_frame(self, last_seq: int, *, timeout_s: float) -> tuple[int, object | None]:
        deadline = time.monotonic() + max(0.01, float(timeout_s))
        with self._cam_frame_cond:
            while self._cam_frame_seq <= last_seq and (not self._cam_af_stop.is_set()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cam_frame_cond.wait(timeout=remaining)
            return self._cam_frame_seq, self._cam_latest_frame

    def _set_controls_connected(self, connected: bool) -> None:
        self.port_combo.configure(state="disabled" if connected else "readonly")
        self.baud_combo.configure(state="disabled" if connected else "normal")
        self.eol_combo.configure(state="disabled" if connected else "readonly")
        self.refresh_btn.configure(state="disabled" if connected else "normal")
        self.detect_btn.configure(state="disabled" if connected else "normal")

        self.connect_btn.configure(text="Disconnect" if connected else "Connect")
        self.send_btn.configure(state="normal" if connected else "disabled")
        self.command_entry.configure(state="normal" if connected else "disabled")

    def _set_camera_controls_connected(self, connected: bool) -> None:
        if self._cam_scanning:
            self.cam_combo.configure(state="disabled")
            self.cam_refresh_btn.configure(state="disabled", text="Scanning…")
            self.cam_connect_btn.configure(state="disabled", text="Connect")
            self.cam_setup_btn.configure(state="disabled")
            self.cam_preview_btn.configure(state="disabled", text="Preview")
            self.cam_af_btn.configure(state="disabled", text="Auto Focus (Z)")
            try:
                self.cam_preview_stop_btn.configure(state="disabled")
            except Exception:
                pass
            return

        if self._cam_connecting:
            self.cam_combo.configure(state="disabled")
            self.cam_refresh_btn.configure(state="disabled")
            self.cam_connect_btn.configure(state="disabled", text="Connecting…")
            self.cam_setup_btn.configure(state="disabled")
            self.cam_preview_btn.configure(state="disabled", text="Preview")
            self.cam_af_btn.configure(state="disabled", text="Auto Focus (Z)")
            try:
                self.cam_preview_stop_btn.configure(state="disabled")
            except Exception:
                pass
            return

        self.cam_combo.configure(state="disabled" if connected else "normal")
        self.cam_refresh_btn.configure(state="disabled" if connected else "normal", text="Scan")

        if self._cam_af_active:
            self.cam_connect_btn.configure(state="disabled", text="Disconnect" if connected else "Connect")
            self.cam_setup_btn.configure(state="disabled")
            self.cam_preview_btn.configure(state="disabled", text="Preview")
            self.cam_af_btn.configure(state="normal", text="Stop AF")
            try:
                self.cam_preview_stop_btn.configure(state="disabled")
            except Exception:
                pass
            return

        self.cam_connect_btn.configure(state="normal", text="Disconnect" if connected else "Connect")
        self.cam_setup_btn.configure(state="normal" if connected else "disabled")

        if self._cam_preview_active:
            self.cam_preview_btn.configure(state="normal", text="Stop Preview")
            try:
                self.cam_preview_stop_btn.configure(state="normal")
            except Exception:
                pass
        else:
            self.cam_preview_btn.configure(state="normal" if connected else "disabled", text="Preview")
            try:
                self.cam_preview_stop_btn.configure(state="normal" if connected else "disabled")
            except Exception:
                pass

        # Autofocus requires both camera + printer (it moves printer Z).
        can_af = connected and (self._worker is not None)
        self.cam_af_btn.configure(state="normal" if can_af else "disabled", text="Auto Focus (Z)")

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
        self._set_camera_controls_connected(self._cam_connected)
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
        self._set_camera_controls_connected(self._cam_connected)
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
                elif kind == "cam-status":
                    self._cam_status_var.set(str(payload))
                elif kind == "cam-log":
                    self._append_log(str(payload))
                elif kind == "cam-opened":
                    cam_idx = "?"
                    info = "?"
                    if isinstance(payload, tuple) and len(payload) == 2:
                        cam_idx, info = payload  # type: ignore[misc]
                    # Fresh session state.
                    self._cam_af_active = False
                    try:
                        self._cam_af_stop.clear()
                    except Exception:
                        pass
                    self._cam_connected = True
                    self._cam_connecting = False
                    self._cam_connected_index = None
                    try:
                        self._cam_connected_index = int(cam_idx)
                    except Exception:
                        self._cam_connected_index = None
                    self._cam_status_var.set(f"Camera: Connected (index {cam_idx}, {info})")
                    self._set_camera_controls_connected(True)

                    if not self._cam_preview_active:
                        self.start_camera_preview()

                    try:
                        idx_i = int(cam_idx)
                    except Exception:
                        idx_i = None
                    if idx_i is not None and idx_i not in self._cam_setup_seen:
                        self._cam_setup_seen.add(idx_i)
                        self.after(0, self.open_camera_setup)
                elif kind == "cam-open-failed":
                    cam_idx = "?"
                    msg = ""
                    if isinstance(payload, tuple) and len(payload) == 2:
                        cam_idx, msg = payload  # type: ignore[misc]
                    self._cam_connected = False
                    self._cam_connecting = False
                    self._cam_connected_index = None
                    self._cam_status_var.set("Camera: Disconnected")
                    self._set_camera_controls_connected(False)
                    messagebox.showerror("Camera", f"Failed to open camera {cam_idx}.\n\n{msg}")
                elif kind == "cam-closed":
                    self._cam_af_active = False
                    try:
                        self._cam_af_stop.set()
                    except Exception:
                        pass
                    self._cam_connected = False
                    self._cam_connecting = False
                    self._cam_connected_index = None
                    if self._cam_preview_active:
                        self.stop_camera_preview(force=True)
                    self._cam_status_var.set("Camera: Disconnected")
                    self._set_camera_controls_connected(False)
                elif kind == "cam-preview-fail":
                    # UI tick reads the fail count; this event is just informational.
                    pass
                elif kind == "cam-scan-done":
                    self._cam_scanning = False
                    items: list[tuple[int, bool, bool, str]] = []
                    if isinstance(payload, list):
                        try:
                            items = [(int(a), bool(b), bool(c), str(d)) for (a, b, c, d) in payload]  # type: ignore[misc]
                        except Exception:
                            items = []

                    values: list[str] = []
                    indices: list[int] = []
                    for idx, opened, frame_ok, info in items:
                        if not opened:
                            continue
                        indices.append(int(idx))
                        suffix = "" if frame_ok else " (no frames)"
                        values.append(f"{idx} - {info or '?'}{suffix}")
                    self._cam_indices = indices
                    try:
                        self.cam_combo["values"] = values
                    except Exception:
                        pass

                    # Keep selection if possible; otherwise pick the first found.
                    current = self._cam_index_var.get().strip()
                    if current and current in values:
                        pass
                    else:
                        cur_idx = self._resolve_camera_index()
                        set_done = False
                        if cur_idx is not None:
                            for v in values:
                                if v.startswith(f"{cur_idx} -"):
                                    self._cam_index_var.set(v)
                                    set_done = True
                                    break
                        if (not set_done) and values:
                            self._cam_index_var.set(values[0])

                    if not values:
                        self._cam_status_var.set("Camera: No devices found (type index or connect a UVC camera).")
                    else:
                        self._cam_status_var.set("Camera: Disconnected")
                    self._set_camera_controls_connected(self._cam_connected)
                elif kind == "cam-scan-failed":
                    self._cam_scanning = False
                    self._cam_status_var.set("Camera: Scan failed.")
                    self._set_camera_controls_connected(self._cam_connected)
                elif kind == "cam-af-finished":
                    ok_flag = False
                    msg = ""
                    if isinstance(payload, tuple) and len(payload) == 2:
                        ok_flag, msg = payload  # type: ignore[misc]
                    else:
                        msg = str(payload)
                    self._cam_af_active = False
                    self._cam_af_stop.clear()
                    self._set_camera_controls_connected(self._cam_connected)
                    if msg:
                        self._cam_status_var.set(f"Camera: {msg}" if not msg.startswith("Camera:") else msg)
                    if (not bool(ok_flag)) and msg.lower().startswith("af failed"):
                        messagebox.showerror("Auto Focus", msg)
                elif kind == "rt-status":
                    try:
                        self._rt_status_var.set(str(payload))
                    except Exception:
                        pass
                elif kind == "rt-redraw":
                    try:
                        self._rt_request_redraw()
                    except Exception:
                        pass
                elif kind == "scan-status":
                    self._scan_status_var.set(str(payload))
                elif kind == "scan-stitch-progress":
                    pct = None
                    msg = None
                    if isinstance(payload, tuple) and len(payload) == 2:
                        pct, msg = payload  # type: ignore[misc]
                    else:
                        msg = str(payload)
                    try:
                        if pct is not None:
                            self._scan_stitch_progress_var.set(float(pct))
                    except Exception:
                        pass
                    try:
                        if msg is not None:
                            self._scan_stitch_progress_text_var.set(str(msg))
                    except Exception:
                        pass
                elif kind == "scan-finished":
                    ok_flag = False
                    msg = ""
                    if isinstance(payload, tuple) and len(payload) == 2:
                        ok_flag, msg = payload  # type: ignore[misc]
                    else:
                        msg = str(payload)
                    self._scan_active = False
                    self._scan_stop.clear()
                    try:
                        self._scan_set_ui_running(False)
                    except Exception:
                        pass
                    if msg:
                        self._scan_status_var.set(str(msg))
                    if (not bool(ok_flag)) and msg and (not msg.lower().startswith("scan stopped")):
                        messagebox.showerror("Scan", msg)
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
            try:
                self._scan_stop.set()
            except Exception:
                pass
            self._cam_af_stop.set()
            self.disconnect_camera(force=True)
            self.disconnect()
        finally:
            try:
                self._cam_worker_stop.set()
            except Exception:
                pass
            self.destroy()


def main() -> int:
    app = PrinterGUI()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
