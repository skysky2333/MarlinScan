from __future__ import annotations

import json
import os
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk

from ..uvc import compute_sharpness, transform_frame


def _fmt_duration(seconds: float | int) -> str:
    try:
        s = max(0, int(round(float(seconds))))
    except Exception:
        return "?"
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


@dataclass(frozen=True)
class ScanParams:
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    step_x_mm: float
    step_y_mm: float
    serpentine: bool
    focus_plane: bool
    mesh_nx: int
    mesh_ny: int
    autofocus_each_tile: bool
    shots_per_tile: int
    stack_mode: str  # "none" | "best" | "nlmeans"
    stitch_method: str  # "bed" | "opencv"
    capture_settle_ms: int  # 0 = auto (derived from fps / camera settle)
    downsample: int  # reserved; currently always 1 (full-res)
    build_pyramidal_tiff: bool
    build_deepzoom: bool
    pyramid_tile_px: int
    tiff_compression: str  # "none" | "lzw" | "deflate"
    out_base_dir: str


class ScanTabMixin:
    def _build_scan_tab(self, parent: ttk.Frame) -> None:
        intro = ttk.LabelFrame(parent, text="Camera Bed Scan (tiles → stitched output)", padding=10)
        intro.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(
            intro,
            text=(
                "Captures full-resolution tiles across the work area. Optional per-tile autofocus.\n"
                "Tiles are saved as uncompressed TIFF. Optionally stitches into an uncompressed pyramidal BigTIFF and/or DeepZoom (PNG).\n"
                "Stitching requires optional dependency: pyvips + libvips."
            ),
            justify=tk.LEFT,
        ).pack(side=tk.TOP, anchor=tk.W)

        area = ttk.LabelFrame(parent, text="Area / Grid (mm)", padding=10)
        area.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))

        ttk.Label(area, text="X min:").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(area, textvariable=self._scan_x_min_var, width=8).grid(row=0, column=1, sticky=tk.W, padx=(6, 12))
        ttk.Label(area, text="X max:").grid(row=0, column=2, sticky=tk.W)
        ttk.Entry(area, textvariable=self._scan_x_max_var, width=8).grid(row=0, column=3, sticky=tk.W, padx=(6, 12))

        ttk.Label(area, text="Y min:").grid(row=0, column=4, sticky=tk.W)
        ttk.Entry(area, textvariable=self._scan_y_min_var, width=8).grid(row=0, column=5, sticky=tk.W, padx=(6, 12))
        ttk.Label(area, text="Y max:").grid(row=0, column=6, sticky=tk.W)
        ttk.Entry(area, textvariable=self._scan_y_max_var, width=8).grid(row=0, column=7, sticky=tk.W, padx=(6, 0))

        ttk.Label(area, text="Step X:").grid(row=1, column=0, sticky=tk.W, pady=(8, 0))
        ttk.Entry(area, textvariable=self._scan_step_x_var, width=8).grid(
            row=1, column=1, sticky=tk.W, padx=(6, 12), pady=(8, 0)
        )
        ttk.Label(area, text="Step Y:").grid(row=1, column=2, sticky=tk.W, pady=(8, 0))
        ttk.Entry(area, textvariable=self._scan_step_y_var, width=8).grid(
            row=1, column=3, sticky=tk.W, padx=(6, 12), pady=(8, 0)
        )
        ttk.Checkbutton(area, text="Serpentine (zig-zag rows)", variable=self._scan_serpentine_var).grid(
            row=1, column=4, columnspan=4, sticky=tk.W, pady=(8, 0)
        )

        cap = ttk.LabelFrame(parent, text="Capture", padding=10)
        cap.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))

        ttk.Checkbutton(
            cap,
            text="Calibrate focus mesh (NxM points) and use it during scan",
            variable=self._scan_focus_plane_var,
        ).grid(row=0, column=0, columnspan=4, sticky=tk.W)

        ttk.Label(cap, text="Mesh:").grid(row=1, column=0, sticky=tk.W, pady=(6, 0))
        ttk.Combobox(
            cap,
            textvariable=self._scan_focus_mesh_var,
            values=["3x3", "4x4", "5x5", "7x7"],
            width=8,
            state="readonly",
        ).grid(row=1, column=1, sticky=tk.W, padx=(6, 12), pady=(6, 0))

        ttk.Checkbutton(cap, text="Autofocus each tile (small local refine; moves Z)", variable=self._scan_af_each_tile_var).grid(
            row=2, column=0, columnspan=4, sticky=tk.W, pady=(6, 0)
        )

        ttk.Label(cap, text="Shots/tile:").grid(row=3, column=0, sticky=tk.W, pady=(8, 0))
        ttk.Entry(cap, textvariable=self._scan_shots_var, width=8).grid(
            row=3, column=1, sticky=tk.W, padx=(6, 12), pady=(8, 0)
        )

        ttk.Label(cap, text="Multi-shot:").grid(row=3, column=2, sticky=tk.W, pady=(8, 0))
        ttk.Combobox(
            cap,
            textvariable=self._scan_stack_var,
            values=["none", "best", "nlmeans"],
            width=10,
            state="readonly",
        ).grid(row=3, column=3, sticky=tk.W, padx=(6, 0), pady=(8, 0))

        ttk.Label(cap, text="Capture settle (ms):").grid(row=4, column=0, sticky=tk.W, pady=(8, 0))
        ttk.Entry(cap, textvariable=self._scan_capture_settle_ms_var, width=8).grid(
            row=4, column=1, sticky=tk.W, padx=(6, 12), pady=(8, 0)
        )
        ttk.Label(cap, text="(0=auto; wait after motion before capture)").grid(
            row=4, column=2, columnspan=2, sticky=tk.W, pady=(8, 0)
        )

        stitch = ttk.LabelFrame(parent, text="Stitching / Output", padding=10)
        stitch.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))

        ttk.Checkbutton(stitch, text="Build pyramidal TIFF (one big file)", variable=self._scan_build_pyramid_var).grid(
            row=0, column=0, columnspan=6, sticky=tk.W
        )
        ttk.Checkbutton(stitch, text="Build DeepZoom tiles (PNG)", variable=self._scan_build_deepzoom_var).grid(
            row=1, column=0, columnspan=6, sticky=tk.W, pady=(6, 0)
        )

        ttk.Label(stitch, text="Stitch method:").grid(row=2, column=0, sticky=tk.W, pady=(8, 0))
        ttk.Combobox(
            stitch,
            textvariable=self._scan_stitch_method_var,
            values=["bed", "opencv"],
            width=12,
            state="readonly",
        ).grid(row=2, column=1, sticky=tk.W, padx=(6, 12), pady=(8, 0))
        ttk.Label(stitch, text="(opencv is experimental; may be slow / skip tiles)").grid(
            row=2, column=2, columnspan=4, sticky=tk.W, pady=(8, 0)
        )

        ttk.Label(stitch, text="TIFF internal tile (px):").grid(row=3, column=0, sticky=tk.W, pady=(8, 0))
        ttk.Combobox(
            stitch,
            textvariable=self._scan_pyramid_tile_var,
            values=["256", "512", "1024", "2048"],
            width=8,
            state="normal",
        ).grid(row=3, column=1, sticky=tk.W, padx=(6, 12), pady=(8, 0))
        ttk.Label(stitch, text="(speed/seek hint; does not change pixel data)").grid(
            row=3, column=2, columnspan=4, sticky=tk.W, pady=(8, 0)
        )

        out = ttk.LabelFrame(parent, text="Output", padding=10)
        out.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))

        ttk.Label(out, text="Folder:").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(out, textvariable=self._scan_out_dir_var, width=60).grid(row=0, column=1, sticky=tk.W, padx=(6, 10))
        ttk.Button(out, text="Choose…", command=self._scan_choose_dir).grid(row=0, column=2, sticky=tk.W)
        out.grid_columnconfigure(1, weight=1)

        btns = ttk.Frame(parent)
        btns.pack(side=tk.TOP, fill=tk.X, pady=(12, 0))

        self.scan_start_btn = ttk.Button(btns, text="Start Scan", command=self._scan_start)
        self.scan_start_btn.pack(side=tk.LEFT)
        self.scan_stop_btn = ttk.Button(btns, text="Stop", command=self._scan_stop_clicked, state="disabled")
        self.scan_stop_btn.pack(side=tk.LEFT, padx=(10, 0))

        ttk.Label(btns, textvariable=self._scan_status_var).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Label(parent, textvariable=self._scan_estimate_var).pack(side=tk.TOP, anchor=tk.W, pady=(8, 0))

        # Live estimate (capture-only) as params change.
        self._scan_estimate_after_id = None

        def _schedule_estimate(*_a: object) -> None:
            if getattr(self, "_scan_active", False):
                return
            after_id = getattr(self, "_scan_estimate_after_id", None)
            if after_id is not None:
                try:
                    self.after_cancel(after_id)
                except Exception:
                    pass
            self._scan_estimate_after_id = self.after(250, _update_estimate)

        def _update_estimate() -> None:
            self._scan_estimate_after_id = None
            try:
                x0 = float(self._scan_x_min_var.get())
                x1 = float(self._scan_x_max_var.get())
                y0 = float(self._scan_y_min_var.get())
                y1 = float(self._scan_y_max_var.get())
                step_x = float(self._scan_step_x_var.get())
                step_y = float(self._scan_step_y_var.get())
                shots = int(float(self._scan_shots_var.get()))
                stack_mode = str(self._scan_stack_var.get()).strip().lower() or "none"
                pyramid_tile_px = int(float(self._scan_pyramid_tile_var.get()))
                mesh_txt = str(self._scan_focus_mesh_var.get()).strip().lower().replace("×", "x")
                capture_settle_ms = int(float(self._scan_capture_settle_ms_var.get()))
                out_base = (self._scan_out_dir_var.get().strip() or "").strip() or os.path.join(os.getcwd(), "scans")
            except Exception:
                self._scan_estimate_var.set("Estimate: —")
                return

            if x1 < x0:
                x0, x1 = x1, x0
            if y1 < y0:
                y0, y1 = y1, y0
            if step_x <= 0 or step_y <= 0:
                self._scan_estimate_var.set("Estimate: —")
                return

            if shots < 1:
                shots = 1
            if stack_mode not in {"none", "best", "nlmeans"}:
                stack_mode = "none"
            pyramid_tile_px = max(128, min(4096, int(pyramid_tile_px)))
            capture_settle_ms = max(0, min(5000, int(capture_settle_ms)))
            downsample = 1
            tiff_comp = "none"
            mesh_nx = 3
            mesh_ny = 3
            try:
                parts = [p.strip() for p in mesh_txt.split("x", 1)]
                if len(parts) == 2:
                    mesh_nx = int(float(parts[0]))
                    mesh_ny = int(float(parts[1]))
            except Exception:
                mesh_nx = 3
                mesh_ny = 3
            mesh_nx = max(2, min(9, int(mesh_nx)))
            mesh_ny = max(2, min(9, int(mesh_ny)))

            params = ScanParams(
                x_min=float(x0),
                x_max=float(x1),
                y_min=float(y0),
                y_max=float(y1),
                step_x_mm=float(step_x),
                step_y_mm=float(step_y),
                serpentine=bool(self._scan_serpentine_var.get()),
                focus_plane=bool(self._scan_focus_plane_var.get()),
                mesh_nx=int(mesh_nx),
                mesh_ny=int(mesh_ny),
                autofocus_each_tile=bool(self._scan_af_each_tile_var.get()),
                shots_per_tile=int(shots),
                stack_mode=str(stack_mode),
                stitch_method=str(getattr(self, "_scan_stitch_method_var", tk.StringVar(value="bed")).get()).strip()
                or "bed",
                capture_settle_ms=int(capture_settle_ms),
                downsample=int(downsample),
                build_pyramidal_tiff=bool(self._scan_build_pyramid_var.get()),
                build_deepzoom=bool(self._scan_build_deepzoom_var.get()),
                pyramid_tile_px=int(pyramid_tile_px),
                tiff_compression=str(tiff_comp),
                out_base_dir=str(out_base),
            )

            est_s = self._scan_estimate_seconds(params)
            self._scan_estimate_var.set(f"Estimate: { _fmt_duration(est_s) } (capture only; stitching extra)")

        for _v in (
            self._scan_x_min_var,
            self._scan_x_max_var,
            self._scan_y_min_var,
            self._scan_y_max_var,
            self._scan_step_x_var,
            self._scan_step_y_var,
            self._scan_serpentine_var,
            self._scan_focus_plane_var,
            self._scan_focus_mesh_var,
            self._scan_af_each_tile_var,
            self._scan_shots_var,
            self._scan_stack_var,
            getattr(self, "_scan_stitch_method_var", tk.StringVar(value="bed")),
            self._scan_capture_settle_ms_var,
            self._scan_build_pyramid_var,
            self._scan_build_deepzoom_var,
            self._scan_pyramid_tile_var,
            self._scan_out_dir_var,
        ):
            try:
                _v.trace_add("write", _schedule_estimate)
            except Exception:
                pass
        _schedule_estimate()

    def _scan_choose_dir(self) -> None:
        try:
            start = self._scan_out_dir_var.get().strip()
        except Exception:
            start = ""
        if not start:
            start = os.getcwd()
        path = filedialog.askdirectory(title="Choose scan output folder", initialdir=start)
        if path:
            self._scan_out_dir_var.set(path)

    def _scan_set_ui_running(self, running: bool) -> None:
        try:
            self.scan_start_btn.configure(state="disabled" if running else "normal")
            self.scan_stop_btn.configure(state="normal" if running else "disabled")
        except Exception:
            pass

    def _scan_start(self) -> None:
        if getattr(self, "_scan_active", False):
            return

        if getattr(self, "_worker", None) is None:
            messagebox.showerror("Scan", "Printer not connected.")
            return
        if not getattr(self, "_cam_connected", False):
            messagebox.showerror("Scan", "Camera not connected.")
            return
        if getattr(self, "_rt_active", False) or getattr(self, "_kb_active", False):
            messagebox.showerror("Scan", "Stop realtime modes before scanning.")
            return
        if getattr(self, "_cam_af_active", False):
            messagebox.showerror("Scan", "Stop Auto Focus before scanning.")
            return

        try:
            x0 = float(self._scan_x_min_var.get())
            x1 = float(self._scan_x_max_var.get())
            y0 = float(self._scan_y_min_var.get())
            y1 = float(self._scan_y_max_var.get())
            step_x = float(self._scan_step_x_var.get())
            step_y = float(self._scan_step_y_var.get())
            shots = int(float(self._scan_shots_var.get()))
            stack_mode = str(self._scan_stack_var.get()).strip().lower() or "none"
            pyramid_tile_px = int(float(self._scan_pyramid_tile_var.get()))
            mesh_txt = str(self._scan_focus_mesh_var.get()).strip().lower().replace("×", "x")
            capture_settle_ms = int(float(self._scan_capture_settle_ms_var.get()))
        except Exception:
            messagebox.showerror("Scan", "One or more scan parameters are invalid.")
            return

        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        if step_x <= 0 or step_y <= 0:
            messagebox.showerror("Scan", "Step X/Y must be > 0.")
            return
        if shots < 1:
            shots = 1
        if stack_mode not in {"none", "best", "nlmeans"}:
            stack_mode = "none"
        pyramid_tile_px = max(128, min(4096, int(pyramid_tile_px)))
        capture_settle_ms = max(0, min(5000, int(capture_settle_ms)))
        downsample = 1
        tiff_comp = "none"
        mesh_nx = 3
        mesh_ny = 3
        try:
            parts = [p.strip() for p in mesh_txt.split("x", 1)]
            if len(parts) == 2:
                mesh_nx = int(float(parts[0]))
                mesh_ny = int(float(parts[1]))
        except Exception:
            mesh_nx = 3
            mesh_ny = 3
        mesh_nx = max(2, min(9, int(mesh_nx)))
        mesh_ny = max(2, min(9, int(mesh_ny)))

        out_base = (self._scan_out_dir_var.get().strip() or "").strip()
        if not out_base:
            out_base = os.path.join(os.getcwd(), "scans")

        params = ScanParams(
            x_min=float(x0),
            x_max=float(x1),
            y_min=float(y0),
            y_max=float(y1),
            step_x_mm=float(step_x),
            step_y_mm=float(step_y),
            serpentine=bool(self._scan_serpentine_var.get()),
            focus_plane=bool(self._scan_focus_plane_var.get()),
            mesh_nx=int(mesh_nx),
            mesh_ny=int(mesh_ny),
            autofocus_each_tile=bool(self._scan_af_each_tile_var.get()),
            shots_per_tile=int(shots),
            stack_mode=str(stack_mode),
            stitch_method=str(getattr(self, "_scan_stitch_method_var", tk.StringVar(value="bed")).get()).strip()
            or "bed",
            capture_settle_ms=int(capture_settle_ms),
            downsample=int(downsample),
            build_pyramidal_tiff=bool(self._scan_build_pyramid_var.get()),
            build_deepzoom=bool(self._scan_build_deepzoom_var.get()),
            pyramid_tile_px=int(pyramid_tile_px),
            tiff_compression=str(tiff_comp),
            out_base_dir=str(out_base),
        )

        est_s = self._scan_estimate_seconds(params)
        self._scan_estimate_var.set(f"Estimate: { _fmt_duration(est_s) } (capture only; stitching extra)")

        if bool(getattr(self, "_confirm_motion_var", tk.BooleanVar(value=True)).get()):
            ok = messagebox.askokcancel(
                "Scan",
                "This will move X/Y across the bed (and optionally Z for autofocus).\n\n"
                "Make sure the nozzle/camera is clear and travel is safe.\n\nContinue?",
            )
            if not ok:
                return

        if not getattr(self, "_cam_preview_active", False):
            try:
                self.start_camera_preview()
            except Exception:
                pass

        self._scan_active = True
        self._scan_stop.clear()
        try:
            self._cam_af_stop.clear()
        except Exception:
            pass
        self._scan_set_ui_running(True)
        self._scan_status_var.set("Scan: starting…")

        t = threading.Thread(target=self._scan_worker, args=(params,), daemon=True)
        t.start()

    def _scan_stop_clicked(self) -> None:
        if not getattr(self, "_scan_active", False):
            return
        self._scan_stop.set()
        try:
            self._cam_af_stop.set()  # best-effort: abort per-tile AF promptly
        except Exception:
            pass
        self._scan_status_var.set("Scan: stopping…")

    def _scan_estimate_seconds(self, params: ScanParams) -> float:
        try:
            speed_xy = float(self._speed_xy_var.get())
        except Exception:
            speed_xy = 150.0
        speed_xy = max(5.0, min(500.0, float(speed_xy)))
        try:
            speed_z = float(self._speed_z_var.get())
        except Exception:
            speed_z = 10.0
        speed_z = max(0.5, float(speed_z))

        try:
            cam_fps = float(getattr(self, "_cam_config").fps)  # type: ignore[attr-defined]
        except Exception:
            cam_fps = 30.0
        cam_fps = max(1.0, cam_fps)

        # Grid size
        nx = max(1, int(round((params.x_max - params.x_min) / params.step_x_mm)) + 1)
        ny = max(1, int(round((params.y_max - params.y_min) / params.step_y_mm)) + 1)
        total = nx * ny

        row_len = max(0.0, float(nx - 1) * float(params.step_x_mm))
        total_x = row_len * float(ny)
        if not params.serpentine:
            total_x += row_len * max(0.0, float(ny - 1))  # return-to-start each row
        total_y = max(0.0, float(ny - 1) * float(params.step_y_mm))
        move_time = (total_x + total_y) / max(1e-6, speed_xy)

        # Per-tile capture overhead: we intentionally drop a few buffered frames after motion,
        # then wait a small settle interval before capturing frames for the tile.
        try:
            cfg_settle_s = float(getattr(self, "_cam_config").af_settle_ms) / 1000.0  # type: ignore[attr-defined]
        except Exception:
            cfg_settle_s = 0.0
        warmup_frames = max(2, min(8, int(round(float(cam_fps) * 0.12))))
        auto_settle_s = max(min(0.2, 1.0 / float(cam_fps)), min(0.2, max(0.0, float(cfg_settle_s))))
        try:
            capture_settle_ms = int(float(getattr(params, "capture_settle_ms", 0)))
        except Exception:
            capture_settle_ms = 0
        capture_settle_ms = max(0, min(5000, int(capture_settle_ms)))
        if capture_settle_ms > 0:
            motion_settle_s = min(5.0, float(capture_settle_ms) / 1000.0)
        else:
            motion_settle_s = float(auto_settle_s)
        motion_settle_s = max(1.0 / float(cam_fps), float(motion_settle_s))

        capture_time = float(total) * (
            (float(warmup_frames) + 1.0 + float(params.shots_per_tile)) / float(cam_fps) + float(motion_settle_s)
        )

        af_time = 0.0
        calib_time = 0.0
        if params.autofocus_each_tile:
            try:
                max_travel = float(getattr(self, "_cam_config").af_max_travel_mm)  # type: ignore[attr-defined]
                slow_step = float(getattr(self, "_cam_config").af_slow_step_mm)  # type: ignore[attr-defined]
            except Exception:
                max_travel = 10.0
                slow_step = 0.1
            max_travel = max(0.0, max_travel)
            slow_step = max(0.001, slow_step)
            # AF is full-range once, then local (tile) for subsequent positions.
            coarse_sweep = (2.0 * max_travel) / speed_z
            fine_span_full = max(6.0 * slow_step, 2.0)
            fine_sweep_full = (2.0 * fine_span_full) / max(0.5, speed_z * 0.25)
            micro_span_full = max(3.0 * slow_step, 0.8)
            micro_sweep_full = (2.0 * micro_span_full) / max(0.5, speed_z * 0.125)
            refine_full = 2.0  # discrete refinement + extra waits
            per_tile_full = coarse_sweep + fine_sweep_full + micro_sweep_full + refine_full

            # "Tile" AF is a single-step probe (1–2 tiny Z moves, 2 focus samples) in the current implementation.
            # Model it as mostly overhead + 2 frames worth of focus metric sampling.
            per_tile_tile = 0.22 + (2.0 / cam_fps)

            full_tiles = 1
            rest_tiles = max(0, int(total) - full_tiles)
            af_time = float(per_tile_full) + (float(rest_tiles) * float(per_tile_tile))
        if params.focus_plane:
            # Focus-mesh calibration (NxM points), full AF each point.
            try:
                max_travel = float(getattr(self, "_cam_config").af_max_travel_mm)  # type: ignore[attr-defined]
                slow_step = float(getattr(self, "_cam_config").af_slow_step_mm)  # type: ignore[attr-defined]
            except Exception:
                max_travel = 10.0
                slow_step = 0.1
            max_travel = max(0.0, max_travel)
            slow_step = max(0.001, slow_step)
            coarse_sweep = (2.0 * max_travel) / speed_z
            fine_span_full = max(6.0 * slow_step, 2.0)
            fine_sweep_full = (2.0 * fine_span_full) / max(0.5, speed_z * 0.25)
            micro_span_full = max(3.0 * slow_step, 0.8)
            micro_sweep_full = (2.0 * micro_span_full) / max(0.5, speed_z * 0.125)
            refine_full = 2.0
            per_cal = coarse_sweep + fine_sweep_full + micro_sweep_full + refine_full
            n_cal = max(0, int(getattr(params, "mesh_nx", 3)) * int(getattr(params, "mesh_ny", 3)))
            calib_time = (float(n_cal) * float(per_cal)) + 8.0

        overhead = float(total) * 0.35  # enqueue/acks, small waits, writes (rough)
        return float(move_time + capture_time + af_time + overhead + calib_time)

    def _scan_worker(self, params: ScanParams) -> None:
        def ev(kind: str, payload: object) -> None:
            try:
                self._events.put((kind, payload))
            except Exception:
                pass

        def status(msg: str) -> None:
            ev("scan-status", msg)

        def finish(ok: bool, msg: str) -> None:
            ev("scan-finished", (bool(ok), str(msg)))

        try:
            import cv2  # type: ignore
            import numpy as np  # type: ignore
        except Exception as exc:
            finish(False, f"Missing dependency: {exc}")
            return

        if getattr(self, "_worker", None) is None:
            finish(False, "Printer disconnected.")
            return
        if not getattr(self, "_cam_connected", False):
            finish(False, "Camera disconnected.")
            return

        ts = time.strftime("%Y%m%d_%H%M%S")
        out_dir = os.path.join(os.path.expanduser(params.out_base_dir), f"scan_{ts}")
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as exc:
            finish(False, f"Failed to create output dir: {exc}")
            return

        try:
            with open(os.path.join(out_dir, "scan_params.json"), "w", encoding="utf-8") as f:
                json.dump(params.__dict__, f, indent=2, sort_keys=True)
        except Exception:
            pass

        x0 = float(params.x_min)
        x1 = float(params.x_max)
        y0 = float(params.y_min)
        y1 = float(params.y_max)
        step_x = float(params.step_x_mm)
        step_y = float(params.step_y_mm)

        xs: list[float] = []
        v = float(x0)
        while v <= (x1 + 1e-6):
            xs.append(float(v))
            v += float(step_x)
        if not xs:
            xs = [float(x0)]
        if xs[-1] < (x1 - 1e-6):
            xs.append(float(x1))

        ys: list[float] = []
        v = float(y0)
        while v <= (y1 + 1e-6):
            ys.append(float(v))
            v += float(step_y)
        if not ys:
            ys = [float(y0)]
        if ys[-1] < (y1 - 1e-6):
            ys.append(float(y1))

        nx = len(xs)
        ny = len(ys)
        total = int(nx * ny)

        tiles: list[dict[str, object]] = []

        restore_coord = "absolute"
        try:
            restore_coord = str(self._coord_mode_var.get())
        except Exception:
            restore_coord = "absolute"

        try:
            speed_xy = float(self._speed_xy_var.get())
        except Exception:
            speed_xy = 150.0
        speed_xy = max(5.0, min(500.0, speed_xy))
        feed_xy = int(round(speed_xy * 60.0))
        try:
            speed_z = float(self._speed_z_var.get())
        except Exception:
            speed_z = 10.0
        speed_z = max(0.5, float(speed_z))
        feed_z = int(round(speed_z * 60.0))

        try:
            z_min = float(self._bed_z_min_var.get())
            z_max = float(self._bed_z_max_var.get())
        except Exception:
            z_min = 0.0
            z_max = 250.0
        if z_max < z_min:
            z_min, z_max = z_max, z_min

        def gcode_wait(cmd: str, *, timeout_s: float, tag_prefix: str) -> None:
            ok, _lines = self._send_and_wait(cmd, timeout_s=float(timeout_s), tag_prefix=tag_prefix, log=False)
            if not ok:
                raise RuntimeError(f"G-code failed: {cmd}")

        # Ensure absolute XY for scan moves.
        try:
            gcode_wait("G90", timeout_s=5.0, tag_prefix="scan_g90")
        except Exception:
            pass

        with self._cam_frame_cond:
            last_seq = int(self._cam_frame_seq)

        try:
            cam_fps = float(getattr(self, "_cam_config").fps)  # type: ignore[attr-defined]
        except Exception:
            cam_fps = 30.0
        cam_fps = max(1.0, float(cam_fps))
        # After motion, UVC pipelines can deliver a few buffered frames that were captured during movement.
        # Drop a short burst before using frames for AF/capture.
        warmup_frames = max(2, min(8, int(round(cam_fps * 0.12))))
        try:
            cfg_settle_s = float(getattr(self, "_cam_config").af_settle_ms) / 1000.0  # type: ignore[attr-defined]
        except Exception:
            cfg_settle_s = 0.0
        # Settle time after motion before trusting captured frames.
        # - If capture_settle_ms is 0, derive a small default from fps + camera AF settle.
        # - If set (>0), use it (still ensure at least one frame interval).
        try:
            capture_settle_ms = int(float(getattr(params, "capture_settle_ms", 0)))
        except Exception:
            capture_settle_ms = 0
        capture_settle_ms = max(0, min(5000, int(capture_settle_ms)))
        auto_settle_s = max(min(0.2, 1.0 / float(cam_fps)), min(0.2, max(0.0, float(cfg_settle_s))))
        if capture_settle_ms > 0:
            motion_settle_s = min(5.0, float(capture_settle_ms) / 1000.0)
        else:
            motion_settle_s = float(auto_settle_s)
        motion_settle_s = max(1.0 / float(cam_fps), float(motion_settle_s))

        def flush_frames(n: int, *, timeout_s: float = 1.0) -> None:
            nonlocal last_seq
            for _i in range(max(0, int(n))):
                if self._scan_stop.is_set():
                    break
                last_seq, _fr = self._camera_wait_for_next_frame(last_seq, timeout_s=float(timeout_s))

        def capture_frames(n: int) -> list[object]:
            nonlocal last_seq
            out: list[object] = []
            need = max(1, int(n))
            flush_frames(int(warmup_frames), timeout_s=1.2)
            if float(motion_settle_s) > 1e-6:
                time.sleep(float(motion_settle_s))
            flush_frames(1, timeout_s=1.2)
            for _i in range(need):
                if self._scan_stop.is_set():
                    break
                last_seq, fr = self._camera_wait_for_next_frame(last_seq, timeout_s=2.5)
                if fr is None:
                    continue
                try:
                    out.append(fr.copy())  # type: ignore[union-attr]
                except Exception:
                    out.append(fr)
            return out

        def align_frames(frames: list[np.ndarray], *, ds: int) -> list[np.ndarray]:
            if len(frames) <= 1:
                return frames
            ref = frames[0]
            h, w = ref.shape[:2]
            ds = max(1, int(ds))
            sw = max(32, w // ds)
            sh = max(32, h // ds)

            ref_g = cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY) if ref.ndim == 3 else ref
            ref_s = cv2.resize(ref_g, (sw, sh), interpolation=cv2.INTER_AREA).astype(np.float32)

            aligned = [ref]
            for img in frames[1:]:
                g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
                g_s = cv2.resize(g, (sw, sh), interpolation=cv2.INTER_AREA).astype(np.float32)
                try:
                    (sx, sy), _resp = cv2.phaseCorrelate(ref_s, g_s)
                except Exception:
                    aligned.append(img)
                    continue
                dx = float(sx) * float(ds)
                dy = float(sy) * float(ds)
                m = np.array([[1.0, 0.0, dx], [0.0, 1.0, dy]], dtype=np.float32)
                try:
                    warped = cv2.warpAffine(
                        img,
                        m,
                        (w, h),
                        flags=cv2.INTER_LINEAR,
                        borderMode=cv2.BORDER_REFLECT,
                    )
                    aligned.append(warped)
                except Exception:
                    aligned.append(img)
            return aligned

        def stack(frames: list[np.ndarray], *, mode: str, ds: int) -> np.ndarray | None:
            if not frames:
                return None
            mode = (mode or "none").strip().lower()
            if mode == "none" or len(frames) == 1:
                return frames[0]

            if mode == "best":
                best = frames[0]
                best_s = None
                for img in frames:
                    try:
                        s = compute_sharpness(img, max_width=None, method="tenengrad")
                    except Exception:
                        continue
                    if best_s is None or float(s) > float(best_s):
                        best_s = float(s)
                        best = img
                return best

            if mode == "nlmeans":
                # Multi-frame denoise. Works best with stable frames; we align first.
                ds_align = max(1, min(4, int(ds)))
                aligned = align_frames(frames, ds=ds_align)
                imgs = aligned
                if len(imgs) >= 3 and hasattr(cv2, "fastNlMeansDenoisingColoredMulti"):
                    win = min(len(imgs), 7)
                    if win % 2 == 0:
                        win -= 1
                    if win < 3:
                        return imgs[0]
                    imgs2 = imgs[:win]
                    idx = win // 2
                    try:
                        return cv2.fastNlMeansDenoisingColoredMulti(
                            imgs2,
                            imgToDenoiseIndex=int(idx),
                            temporalWindowSize=int(win),
                            h=6,
                            hColor=6,
                            templateWindowSize=7,
                            searchWindowSize=21,
                        )
                    except Exception:
                        return imgs2[idx]
                # Fallback: pick best frame (still benefits from multi-shot capture).
                return stack(imgs, mode="best", ds=ds)

            return frames[0]

        start_t = time.monotonic()
        done = 0
        status(f"Scan: {nx}×{ny} = {total} tiles (full-res)")
        z_hint: float | None = None

        mesh_xs: list[float] = []
        mesh_ys: list[float] = []
        mesh_z: list[list[float]] = []

        def mesh_predict(x_mm: float, y_mm: float) -> float | None:
            if not mesh_xs or not mesh_ys or not mesh_z:
                return None
            nxm = len(mesh_xs)
            nym = len(mesh_ys)
            if nxm < 2 or nym < 2:
                return None
            x_c = max(float(mesh_xs[0]), min(float(mesh_xs[-1]), float(x_mm)))
            y_c = max(float(mesh_ys[0]), min(float(mesh_ys[-1]), float(y_mm)))

            dx = float(mesh_xs[-1]) - float(mesh_xs[0])
            dy = float(mesh_ys[-1]) - float(mesh_ys[0])
            if abs(dx) <= 1e-9:
                tx_f = 0.0
            else:
                tx_f = ((x_c - float(mesh_xs[0])) / float(dx)) * float(nxm - 1)
            if abs(dy) <= 1e-9:
                ty_f = 0.0
            else:
                ty_f = ((y_c - float(mesh_ys[0])) / float(dy)) * float(nym - 1)

            i = int(max(0, min(nxm - 2, int(tx_f))))
            j = int(max(0, min(nym - 2, int(ty_f))))
            tx = float(tx_f) - float(i)
            ty = float(ty_f) - float(j)

            z00 = float(mesh_z[j][i])
            z10 = float(mesh_z[j][i + 1])
            z01 = float(mesh_z[j + 1][i])
            z11 = float(mesh_z[j + 1][i + 1])
            return float(((1.0 - tx) * (1.0 - ty) * z00) + (tx * (1.0 - ty) * z10) + ((1.0 - tx) * ty * z01) + (tx * ty * z11))

        if params.focus_plane:
            mx = max(2, int(params.mesh_nx))
            my = max(2, int(params.mesh_ny))

            status(f"Scan: focus-mesh calibration ({mx}×{my})…")

            def _linspace(a: float, b: float, n: int) -> list[float]:
                if n <= 1:
                    return [float((float(a) + float(b)) * 0.5)]
                step = (float(b) - float(a)) / float(n - 1)
                return [float(float(a) + (float(i) * float(step))) for i in range(int(n))]

            mesh_xs = _linspace(float(x0), float(x1), int(mx))
            mesh_ys = _linspace(float(y0), float(y1), int(my))
            mesh_z_opt: list[list[float | None]] = [[None for _ in range(int(mx))] for _ in range(int(my))]

            pts_xyz: list[tuple[float, float, float]] = []
            for j, yy in enumerate(mesh_ys):
                cols = list(range(int(mx)))
                if params.serpentine and (j % 2 == 1):
                    cols.reverse()
                for ii, i in enumerate(cols):
                    if self._scan_stop.is_set():
                        raise InterruptedError("stopped")
                    xx = float(mesh_xs[int(i)])
                    status(f"Scan: focus mesh ({j+1}/{my}) ({ii+1}/{mx})  X={xx:.2f} Y={yy:.2f}")
                    gcode_wait(f"G0 X{xx:g} Y{yy:g} F{feed_xy}", timeout_s=12.0, tag_prefix="scan_cal_xy")
                    gcode_wait("M400", timeout_s=300.0, tag_prefix="scan_cal_m400_xy")
                    if float(motion_settle_s) > 1e-6:
                        time.sleep(float(motion_settle_s))
                    flush_frames(int(warmup_frames), timeout_s=1.2)

                    try:
                        self._cam_af_stop.clear()
                    except Exception:
                        pass

                    # For calibration, use the same autofocus algorithm as the manual "Auto Focus" button.
                    ok_af, z_af, _f, _msg = self._camera_autofocus_thread(
                        "absolute",
                        float(z_min),
                        float(z_max),
                        float(speed_z),
                        emit_events=False,
                        profile="full",
                        start_z_hint=(float(z_hint) if (z_hint is not None) else None),
                    )
                    if (not ok_af) or (z_af is None):
                        status("Scan: focus mesh AF failed; retrying with fresh position…")
                        ok_af, z_af, _f, _msg = self._camera_autofocus_thread(
                            "absolute",
                            float(z_min),
                            float(z_max),
                            float(speed_z),
                            emit_events=False,
                            profile="full",
                            start_z_hint=None,
                        )

                    if ok_af and z_af is not None:
                        z_val = float(z_af)
                        mesh_z_opt[int(j)][int(i)] = float(z_val)
                        pts_xyz.append((float(xx), float(yy), float(z_val)))
                        z_hint = float(z_val)
                    else:
                        status("Scan: focus mesh AF failed (point left blank)")

            # Fill missing mesh points with a best-fit plane (fallback), so interpolation is always defined.
            plane: tuple[float, float, float] | None = None  # z = ax + by + c
            if len(pts_xyz) >= 3:
                try:
                    A = np.array([[x, y, 1.0] for (x, y, _z) in pts_xyz], dtype=np.float64)
                    b = np.array([z for (_x, _y, z) in pts_xyz], dtype=np.float64)
                    coeff, _res, _rank, _s = np.linalg.lstsq(A, b, rcond=None)
                    plane = (float(coeff[0]), float(coeff[1]), float(coeff[2]))
                except Exception:
                    plane = None

            if plane is None and pts_xyz:
                # Fallback to the last known Z.
                try:
                    last_z = float(pts_xyz[-1][2])
                except Exception:
                    last_z = 0.0
                plane = (0.0, 0.0, float(last_z))

            if plane is not None:
                a, b2, c = plane
                mesh_z = []
                for j, yy in enumerate(mesh_ys):
                    row: list[float] = []
                    for i, xx in enumerate(mesh_xs):
                        z0 = mesh_z_opt[int(j)][int(i)]
                        if z0 is None:
                            z0 = (float(a) * float(xx)) + (float(b2) * float(yy)) + float(c)
                        row.append(float(z0))
                    mesh_z.append(row)

                try:
                    with open(os.path.join(out_dir, "focus_mesh.json"), "w", encoding="utf-8") as f:
                        json.dump(
                            {
                                "mesh_nx": int(mx),
                                "mesh_ny": int(my),
                                "x_mm": [float(v) for v in mesh_xs],
                                "y_mm": [float(v) for v in mesh_ys],
                                "z_mm": [[float(v) for v in row] for row in mesh_z],
                                "plane_fallback": {"a": float(a), "b": float(b2), "c": float(c)},
                            },
                            f,
                            indent=2,
                            sort_keys=True,
                        )
                except Exception:
                    pass
                status(f"Scan: focus mesh ready ({mx}×{my})")
            else:
                status("Scan: focus mesh calibration failed (need ≥3 good points); continuing without mesh")

        try:
            for r, y in enumerate(ys):
                if self._scan_stop.is_set():
                    raise InterruptedError("stopped")
                row_xs = list(xs)
                if params.serpentine and (r % 2 == 1):
                    row_xs.reverse()
                for c_idx, x in enumerate(row_xs):
                    if self._scan_stop.is_set():
                        raise InterruptedError("stopped")
                    done += 1

                    elapsed = time.monotonic() - start_t
                    avg = elapsed / max(1, done)
                    rem = avg * max(0, total - done)
                    status(f"Scan: tile {done}/{total}  X={x:.2f} Y={y:.2f}  ETA {_fmt_duration(rem)}")

                    gcode_wait(f"G0 X{x:g} Y{y:g} F{feed_xy}", timeout_s=12.0, tag_prefix="scan_g0")
                    gcode_wait("M400", timeout_s=300.0, tag_prefix="scan_m400_xy")
                    if float(motion_settle_s) > 1e-6:
                        time.sleep(float(motion_settle_s))
                    flush_frames(int(warmup_frames), timeout_s=1.2)

                    z_pred = mesh_predict(float(x), float(y))
                    if z_pred is not None:
                        z_pred = max(float(z_min), min(float(z_max), float(z_pred)))
                        if z_hint is None or abs(float(z_pred) - float(z_hint)) >= 0.002:
                            gcode_wait(f"G0 Z{z_pred:g} F{feed_z}", timeout_s=12.0, tag_prefix="scan_g0z")
                            gcode_wait("M400", timeout_s=300.0, tag_prefix="scan_m400_z")
                            if float(motion_settle_s) > 1e-6:
                                time.sleep(float(motion_settle_s))
                            flush_frames(int(warmup_frames), timeout_s=1.2)
                        z_hint = float(z_pred)

                    if params.autofocus_each_tile:
                        if self._scan_stop.is_set():
                            raise InterruptedError("stopped")
                        try:
                            self._cam_af_stop.clear()
                        except Exception:
                            pass

                        # Quick local refine around the predicted/last focus Z, using the same autofocus
                        # algorithm as the manual button but in a small Z window.
                        z_center = float(z_hint) if (z_hint is not None) else None
                        try:
                            slow_step_cfg = float(getattr(self, "_cam_config").af_slow_step_mm)  # type: ignore[attr-defined]
                        except Exception:
                            slow_step_cfg = 0.1
                        tile_span = max(0.18, min(0.6, max(0.25, 4.0 * float(slow_step_cfg))))

                        if z_center is None:
                            ok_af, _z, _f, _msg = self._camera_autofocus_thread(
                                "absolute",
                                float(z_min),
                                float(z_max),
                                float(speed_z),
                                emit_events=False,
                                profile="full",
                                start_z_hint=None,
                            )
                        else:
                            z_lo = max(float(z_min), float(z_center) - float(tile_span))
                            z_hi = min(float(z_max), float(z_center) + float(tile_span))
                            ok_af, _z, _f, _msg = self._camera_autofocus_thread(
                                "absolute",
                                float(z_lo),
                                float(z_hi),
                                float(speed_z),
                                emit_events=False,
                                profile="tile",
                                start_z_hint=float(z_center),
                            )
                        if not ok_af:
                            status("Scan: autofocus failed (continuing)…")
                        if ok_af and _z is not None:
                            z_hint = float(_z)
                        if float(motion_settle_s) > 1e-6:
                            time.sleep(float(motion_settle_s))
                        flush_frames(int(warmup_frames), timeout_s=1.2)

                    raw_frames = capture_frames(int(params.shots_per_tile))
                    if not raw_frames:
                        status("Scan: no frames (tile skipped)")
                        continue

                    cfg = getattr(self, "_cam_config", None)
                    tf: list[np.ndarray] = []
                    for fr in raw_frames:
                        try:
                            img = transform_frame(
                                fr,
                                rotation_deg=int(getattr(cfg, "rotation_deg", 0)),
                                crop_left_pct=float(getattr(cfg, "crop_left_pct", 0.0)),
                                crop_top_pct=float(getattr(cfg, "crop_top_pct", 0.0)),
                                crop_right_pct=float(getattr(cfg, "crop_right_pct", 0.0)),
                                crop_bottom_pct=float(getattr(cfg, "crop_bottom_pct", 0.0)),
                                max_width=None,
                            )
                        except Exception:
                            img = fr
                        try:
                            tf.append(img.copy())  # type: ignore[union-attr]
                        except Exception:
                            tf.append(img)  # type: ignore[arg-type]

                    tile = stack(tf, mode=str(params.stack_mode), ds=int(params.downsample))
                    if tile is None:
                        continue

                    col = (nx - 1 - c_idx) if (params.serpentine and (r % 2 == 1)) else c_idx
                    filename = f"tile_r{r:03d}_c{col:03d}_x{x:.2f}_y{y:.2f}.tif"
                    path = os.path.join(out_dir, filename)
                    try:
                        # Save lossless/uncompressed (best-effort; depends on OpenCV build).
                        params_write: list[int] = []
                        if hasattr(cv2, "IMWRITE_TIFF_COMPRESSION"):
                            # 1 = no compression (TIFF spec).
                            params_write = [int(cv2.IMWRITE_TIFF_COMPRESSION), 1]
                        cv2.imwrite(path, tile, params_write)
                    except Exception as exc:
                        status(f"Scan: failed to write tile: {exc}")
                        continue

                    tiles.append({"row": int(r), "col": int(col), "x_mm": float(x), "y_mm": float(y), "file": filename})

            try:
                with open(os.path.join(out_dir, "tiles.json"), "w", encoding="utf-8") as f:
                    json.dump(tiles, f, indent=2, sort_keys=False)
            except Exception:
                pass

            if (params.build_pyramidal_tiff or params.build_deepzoom) and tiles:
                stitch_ok = True
                status("Scan: stitching (this can take a while)…")
                try:
                    self._scan_stitch_outputs(
                        tiles=tiles,
                        out_dir=out_dir,
                        downsample=int(params.downsample),
                        build_pyramidal_tiff=bool(params.build_pyramidal_tiff),
                        build_deepzoom=bool(params.build_deepzoom),
                        pyramid_tile_px=int(params.pyramid_tile_px),
                        tiff_compression=str(params.tiff_compression),
                        stitch_method=str(params.stitch_method),
                    )
                    try:
                        with open(os.path.join(out_dir, "stitch_meta.json"), "r", encoding="utf-8") as f:
                            meta = json.load(f)
                        if (
                            str(meta.get("requested_method", "")).strip().lower() == "opencv"
                            and str(meta.get("method", "")).strip().lower() == "bed"
                        ):
                            status("Scan: OpenCV stitch failed; used bed fallback (see stitch_error.txt).")
                    except Exception:
                        pass
                except Exception as exc:
                    stitch_ok = False
                    try:
                        import traceback

                        with open(os.path.join(out_dir, "stitch_error.txt"), "a", encoding="utf-8") as f:
                            f.write(f"[scan_worker] {exc}\n")
                            f.write(traceback.format_exc())
                            f.write("\n\n")
                    except Exception:
                        pass
                    status(f"Scan: stitching failed (tiles still saved): {exc} (see stitch_error.txt)")
                if stitch_ok:
                    status("Scan: stitching done.")
                else:
                    status("Scan: stitching failed (you can restitch later).")

            if (params.build_pyramidal_tiff or params.build_deepzoom) and tiles and not stitch_ok:
                finish(
                    True,
                    f"Scan complete: {len(tiles)}/{total} tiles saved to {out_dir} (stitching failed; see stitch_error.txt)",
                )
            else:
                finish(True, f"Scan complete: {len(tiles)}/{total} tiles saved to {out_dir}")
            return
        except InterruptedError:
            finish(False, "Scan stopped.")
            return
        except Exception as exc:
            finish(False, f"Scan failed: {exc}")
            return
        finally:
            try:
                if restore_coord == "relative":
                    gcode_wait("G91", timeout_s=3.0, tag_prefix="scan_restore_g91")
                else:
                    gcode_wait("G90", timeout_s=3.0, tag_prefix="scan_restore_g90")
            except Exception:
                pass
            try:
                self._cam_af_stop.clear()
            except Exception:
                pass

    def _scan_stitch_outputs(
        self,
        *,
        tiles: list[dict[str, object]],
        out_dir: str,
        downsample: int,
        build_pyramidal_tiff: bool,
        build_deepzoom: bool,
        pyramid_tile_px: int,
        tiff_compression: str,
        stitch_method: str,
    ) -> None:
        import math
        import traceback

        try:
            import numpy as np  # type: ignore
        except Exception:
            return

        method = (stitch_method or "bed").strip().lower()
        if method not in {"bed", "opencv"}:
            method = "bed"
        requested_method = str(method)

        pyvips = None
        try:
            import pyvips as _pyvips  # type: ignore

            pyvips = _pyvips
        except Exception as exc:
            # Best-effort: on macOS, Homebrew installs libvips into /opt/homebrew or /usr/local, which
            # may not be on the dynamic loader path for a Conda Python. Try:
            # - extending DYLD_FALLBACK_LIBRARY_PATH
            # - preloading libvips dylibs with RTLD_GLOBAL
            try:
                import sys

                if sys.platform == "darwin":
                    import ctypes
                    import glob

                    candidates: list[str] = []
                    extra_dirs: list[str] = []
                    for d in (
                        os.path.join(sys.prefix, "lib"),
                        os.path.join(sys.prefix, "Library", "lib"),
                        "/opt/homebrew/lib",
                        "/opt/homebrew/opt/vips/lib",
                        "/opt/homebrew/Cellar/vips/*/lib",
                        "/usr/local/lib",
                        "/usr/local/opt/vips/lib",
                        "/usr/local/Cellar/vips/*/lib",
                    ):
                        try:
                            if "*" in d:
                                for dd in glob.glob(d):
                                    extra_dirs.append(dd)
                                    candidates.extend(glob.glob(os.path.join(dd, "libvips*.dylib")))
                            else:
                                extra_dirs.append(d)
                                candidates.extend(glob.glob(os.path.join(d, "libvips*.dylib")))
                        except Exception:
                            continue

                    # Ensure dylib search paths include likely Homebrew locations.
                    try:
                        env_key = "DYLD_FALLBACK_LIBRARY_PATH"
                        cur = os.environ.get(env_key, "")
                        cur_parts = [p for p in cur.split(":") if p]
                        for d in extra_dirs:
                            if d and (d not in cur_parts) and os.path.isdir(d):
                                cur_parts.append(d)
                        os.environ[env_key] = ":".join(cur_parts)
                    except Exception:
                        pass

                    for p in sorted(set(candidates), reverse=True):
                        try:
                            ctypes.CDLL(p, mode=getattr(ctypes, "RTLD_GLOBAL", 0))
                        except Exception:
                            continue
                    import pyvips as _pyvips  # type: ignore

                    pyvips = _pyvips
            except Exception:
                pyvips = None

            if pyvips is None:
                raise RuntimeError(
                    "pyvips/libvips not available; skipping stitching.\n\n"
                    "Recommended install (Conda):\n"
                    "- `conda install -c conda-forge libvips pyvips`\n\n"
                    "Or (Homebrew + pip):\n"
                    "- `brew install vips`\n"
                    "- `python -m pip install pyvips`\n\n"
                    "If you are using Conda + Homebrew on macOS and libvips still fails to load, try:\n"
                    "- `export DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib:/opt/homebrew/opt/vips/lib:$DYLD_FALLBACK_LIBRARY_PATH`\n\n"
                    f"Import error: {exc}"
                ) from exc

        ds = max(1, int(downsample))

        def _append_err(stage: str, exc: Exception) -> None:
            try:
                with open(os.path.join(out_dir, "stitch_error.txt"), "a", encoding="utf-8") as f:
                    f.write(f"[{stage}] {exc}\n")
                    f.write(traceback.format_exc())
                    f.write("\n\n")
            except Exception:
                pass

        def _write_deepzoom_viewer() -> None:
            """Write a tiny OpenSeadragon viewer HTML next to the scan outputs."""
            try:
                html_path = os.path.join(out_dir, "deepzoom_viewer.html")
                dzi_rel = "deepzoom/mosaic.dzi"
                html = f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>DeepZoom Viewer</title>
    <style>
      html, body, #osd {{ width: 100%; height: 100%; margin: 0; background: #111; }}
    </style>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/openseadragon/4.1.0/openseadragon.min.js"></script>
  </head>
  <body>
    <div id="osd"></div>
    <script>
      OpenSeadragon({{
        id: "osd",
        prefixUrl: "https://cdnjs.cloudflare.com/ajax/libs/openseadragon/4.1.0/images/",
        tileSources: "{dzi_rel}",
        showNavigator: true
      }});
    </script>
  </body>
</html>
"""
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(html)
            except Exception:
                pass

        by_rc: dict[tuple[int, int], dict[str, object]] = {}
        max_r = 0
        max_c = 0
        for t in tiles:
            try:
                r = int(t["row"])
                c = int(t["col"])
                fn = str(t["file"])
                x_mm = float(t.get("x_mm", c))
                y_mm = float(t.get("y_mm", r))
            except Exception:
                continue
            by_rc[(r, c)] = {"path": os.path.join(out_dir, fn), "x_mm": float(x_mm), "y_mm": float(y_mm)}
            max_r = max(max_r, r)
            max_c = max(max_c, c)

        nrows = max_r + 1
        ncols = max_c + 1
        if nrows <= 0 or ncols <= 0:
            return

        # Optional OpenCV acceleration for registration.
        try:
            import cv2  # type: ignore
        except Exception:
            cv2 = None  # type: ignore[assignment]

        reg_max_w = 960
        gray_cache: dict[str, tuple[np.ndarray, float]] = {}

        def _load_gray(path: str) -> tuple[np.ndarray, float] | None:
            if cv2 is None:
                return None
            hit = gray_cache.get(path)
            if hit is not None:
                return hit
            a = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
            if a is None:
                return None
            h, w = a.shape[:2]
            scale = 1.0
            if w > int(reg_max_w):
                scale = float(reg_max_w) / float(w)
                new_w = max(64, int(round(float(w) * float(scale))))
                new_h = max(64, int(round(float(h) * float(scale))))
                a = cv2.resize(a, (new_w, new_h), interpolation=cv2.INTER_AREA)
            gray_cache[path] = (a, float(scale))
            return gray_cache[path]

        def _estimate_shift_px(path_a: str, path_b: str) -> tuple[float, float, int] | None:
            # Estimate A->B translation in full-resolution pixels (x right, y down).
            #
            # Prefer OpenCV ORB when available; fall back to scikit-image ORB.
            try:
                if cv2 is None:
                    raise RuntimeError("cv2 unavailable")
                la = _load_gray(path_a)
                lb = _load_gray(path_b)
                if la is None or lb is None:
                    return None
                a, scale_a = la
                b, scale_b = lb

                h, w = a.shape[:2]
                if b.shape[:2] != (h, w) or abs(float(scale_a) - float(scale_b)) > 1e-6:
                    return None

                scale = float(scale_a)
                orb = cv2.ORB_create(nfeatures=3000, fastThreshold=10)
                kp_a, des_a = orb.detectAndCompute(a, None)
                kp_b, des_b = orb.detectAndCompute(b, None)
                if des_a is None or des_b is None or kp_a is None or kp_b is None:
                    return None
                if len(kp_a) < 12 or len(kp_b) < 12:
                    return None

                matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
                matches = matcher.match(des_a, des_b)
                if not matches or len(matches) < 20:
                    return None
                matches = sorted(matches, key=lambda m: float(getattr(m, "distance", 0.0)))[:400]

                deltas: list[tuple[float, float]] = []
                for m in matches:
                    try:
                        xa, ya = kp_a[int(m.queryIdx)].pt
                        xb, yb = kp_b[int(m.trainIdx)].pt
                    except Exception:
                        continue
                    deltas.append(((float(xb) - float(xa)) / float(scale), (float(yb) - float(ya)) / float(scale)))

                if len(deltas) < 20:
                    return None
                arr = np.array(deltas, dtype=np.float64)
                med = np.median(arr, axis=0)
                mad = np.median(np.abs(arr - med), axis=0) + 1e-6
                z = np.max(np.abs((arr - med) / mad), axis=1)
                keep = z < 5.0
                keep_n = int(keep.sum())
                if keep_n >= 20:
                    med = np.median(arr[keep], axis=0)
                return (float(med[0]), float(med[1]), int(keep_n))
            except Exception:
                pass

            try:
                import tifffile  # type: ignore
                from skimage.color import rgb2gray  # type: ignore
                from skimage.feature import ORB, match_descriptors  # type: ignore
                from skimage.transform import rescale  # type: ignore

                im_a = tifffile.imread(path_a)
                im_b = tifffile.imread(path_b)
                if im_a is None or im_b is None:
                    return None
                if getattr(im_a, "shape", None) != getattr(im_b, "shape", None):
                    return None
                if im_a.ndim == 3:
                    im_a = rgb2gray(im_a)
                if im_b.ndim == 3:
                    im_b = rgb2gray(im_b)
                a2 = im_a.astype(np.float32)
                b2 = im_b.astype(np.float32)
                a2 = (a2 - float(a2.min())) / max(1e-6, float(a2.max() - a2.min()))
                b2 = (b2 - float(b2.min())) / max(1e-6, float(b2.max() - b2.min()))

                h, w = a2.shape[:2]
                reg_max_w = 960
                scale = 1.0
                if w > reg_max_w:
                    scale = float(reg_max_w) / float(w)
                if float(scale) < 0.999:
                    a2s = rescale(a2, float(scale), anti_aliasing=True, preserve_range=True)
                    b2s = rescale(b2, float(scale), anti_aliasing=True, preserve_range=True)
                else:
                    a2s = a2
                    b2s = b2

                orb = ORB(n_keypoints=2000, fast_threshold=0.05)
                try:
                    orb.detect_and_extract(a2s)
                    ka = orb.keypoints
                    da = orb.descriptors
                    orb.detect_and_extract(b2s)
                    kb = orb.keypoints
                    db = orb.descriptors
                except Exception:
                    return None
                if da is None or db is None or len(ka) < 20 or len(kb) < 20:
                    return None
                matches = match_descriptors(da, db, cross_check=True)
                if matches is None or len(matches) < 30:
                    return None
                matches = matches[:800]
                src = ka[matches[:, 0]][:, ::-1]  # (x, y)
                dst = kb[matches[:, 1]][:, ::-1]
                deltas = (dst - src) / float(scale)
                med = np.median(deltas, axis=0)
                mad = np.median(np.abs(deltas - med), axis=0) + 1e-6
                z = np.max(np.abs((deltas - med) / mad), axis=1)
                keep = z < 5.0
                keep_n = int(keep.sum())
                if keep_n >= 30:
                    med = np.median(deltas[keep], axis=0)
                return (float(med[0]), float(med[1]), int(keep_n))
            except Exception:
                return None

        # Load one tile to get tile size/bands.
        any_path = str(next(iter(by_rc.values()))["path"])
        # Use random access here because downstream writers (pyramids / DeepZoom) may request scanlines
        # out-of-order; sequential access can trigger libvips "out of order read" errors.
        im0 = pyvips.Image.new_from_file(any_path, access="random")
        bands = int(im0.bands)
        if bands < 1:
            raise RuntimeError("Tile image has no bands.")
        tile_w = int(im0.width)
        tile_h = int(im0.height)

        if method == "opencv":
            try:
                from stitching import AffineStitcher  # type: ignore
            except Exception as exc:
                raise RuntimeError(
                    "OpenCV stitch method selected, but the 'stitching' package is not installed.\n"
                    "Install with:\n"
                    "- `python -m pip install stitching`"
                ) from exc

            paths = [str(by_rc[rc]["path"]) for rc in sorted(by_rc.keys(), key=lambda rc: (rc[0], rc[1]))]

            if cv2 is None:
                raise RuntimeError("OpenCV (cv2) is required for the OpenCV stitch method.")

            # Best-effort settings tuned for planar (scanner-like) mosaics.
            # Note: This can be slow for large grids and may skip tiles if they have too little texture.
            range_width = max(3, min(int(ncols) + 1, max(3, len(paths) - 1)))
            settings_sift = {
                "detector": "sift",
                "nfeatures": 8000,
                "confidence_threshold": 0.10,
                "range_width": int(range_width),
                "crop": False,
                "compensator": "no",
                "blender_type": "multiband",
                "blend_strength": 5,
                "medium_megapix": 100.0,  # keep full-res features (no upscaling)
                "low_megapix": 0.2,
                "final_megapix": -1,
            }
            settings_orb = {
                "detector": "orb",
                "nfeatures": 12000,
                "confidence_threshold": 0.05,
                "range_width": int(range_width),
                "crop": False,
                "compensator": "no",
                "blender_type": "multiband",
                "blend_strength": 5,
                "medium_megapix": 100.0,  # keep full-res features (no upscaling)
                "low_megapix": 0.2,
                "final_megapix": -1,
            }

            pano_bgr = None
            settings_used: dict[str, object] | None = None
            dropped_note: dict[str, object] = {}

            def _is_flann_knn_error(exc: Exception) -> bool:
                msg = str(exc)
                return ("knn <= index_->size" in msg) or ("runKnnSearch_" in msg)

            def _filter_low_feature_paths(
                paths_in: list[str], *, settings_in: dict[str, object]
            ) -> tuple[list[str], list[str]]:
                """Drop tiles with too-few detected features (prevents OpenCV FLANN knn assertion)."""
                keep: list[str] = []
                drop: list[str] = []

                try:
                    from stitching.feature_detector import FeatureDetector  # type: ignore
                    from stitching.images import Images  # type: ignore

                    det_name = str(settings_in.get("detector", "")).strip().lower() or "orb"
                    nfeat = int(settings_in.get("nfeatures", 5000))
                    med_mp = float(settings_in.get("medium_megapix", 100.0))
                    low_mp = float(settings_in.get("low_megapix", 0.2))
                    fin_mp = float(settings_in.get("final_megapix", -1))

                    imgs = Images.of(paths_in, medium_megapix=float(med_mp), low_megapix=float(low_mp), final_megapix=float(fin_mp))
                    med_iter = imgs.resize(Images.Resolution.MEDIUM)
                    finder = (
                        FeatureDetector(det_name, nfeatures=int(nfeat))
                        if det_name in {"orb", "sift"}
                        else FeatureDetector(det_name)
                    )

                    for p, img in zip(paths_in, med_iter, strict=False):
                        try:
                            feats = finder.detect_features(img)
                            n = len(feats.getKeypoints())
                        except Exception:
                            n = 0
                        if int(n) < 2:
                            drop.append(p)
                        else:
                            keep.append(p)
                    return (keep, drop)
                except Exception:
                    # Fallback: quick grayscale feature count (may be less accurate than stitching's pipeline).
                    det_name = str(settings_in.get("detector", "")).strip().lower() or "orb"
                    try:
                        nfeat = int(settings_in.get("nfeatures", 5000))
                    except Exception:
                        nfeat = 5000
                    det = None
                    try:
                        if det_name == "sift":
                            det = cv2.SIFT_create(nfeatures=int(nfeat))
                        else:
                            det = cv2.ORB_create(nfeatures=int(nfeat), fastThreshold=5)
                    except Exception:
                        det = None

                    max_w = 1600
                    for p in paths_in:
                        img = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
                        if img is None or det is None:
                            drop.append(p)
                            continue
                        h, w = img.shape[:2]
                        if w > int(max_w):
                            scale = float(max_w) / float(w)
                            new_w = max(64, int(round(float(w) * float(scale))))
                            new_h = max(64, int(round(float(h) * float(scale))))
                            img = cv2.resize(img, (int(new_w), int(new_h)), interpolation=cv2.INTER_AREA)
                        try:
                            _kps, des = det.detectAndCompute(img, None)
                        except Exception:
                            des = None
                        if des is None or int(len(des)) < 2:
                            drop.append(p)
                        else:
                            keep.append(p)
                    return (keep, drop)

            def _record_dropped(detector_name: str, dropped: list[str], kept: list[str]) -> None:
                if not dropped:
                    return
                try:
                    dropped_note[detector_name] = {
                        "dropped_count": int(len(dropped)),
                        "kept_count": int(len(kept)),
                        "dropped_files": [os.path.basename(p) for p in dropped],
                    }
                    with open(os.path.join(out_dir, "opencv_dropped_tiles.json"), "w", encoding="utf-8") as f:
                        json.dump(dropped_note, f, indent=2, sort_keys=True)
                except Exception:
                    pass

            def _try_stitch(settings_in: dict[str, object], label: str) -> tuple[object | None, dict[str, object] | None]:
                nonlocal pano_bgr
                try:
                    stitcher = AffineStitcher(**settings_in)
                    pano_bgr = stitcher.stitch(paths)
                    return pano_bgr, settings_in
                except Exception as exc:
                    _append_err(label, exc)
                    if _is_flann_knn_error(exc):
                        det_name = str(settings_in.get("detector", "")).strip().lower() or "orb"
                        kept, dropped = _filter_low_feature_paths(paths, settings_in=settings_in)
                        _record_dropped(det_name, dropped, kept)
                        if len(kept) >= 2 and len(kept) < len(paths):
                            try:
                                stitcher = AffineStitcher(**settings_in)
                                pano_bgr = stitcher.stitch(kept)
                                return pano_bgr, settings_in
                            except Exception as exc2:
                                _append_err(f"{label}_filtered", exc2)
                    return None, None

            pano_bgr, settings_used = _try_stitch(settings_sift, "opencv_stitch_sift")
            if pano_bgr is None:
                pano_bgr, settings_used = _try_stitch(settings_orb, "opencv_stitch_orb")

            if pano_bgr is None:
                # Fall back to the bed-based stitcher so the user still gets outputs.
                _append_err(
                    "opencv_stitch",
                    RuntimeError("OpenCV stitch failed; falling back to bed stitch method."),
                )
                method = "bed"
            else:
                settings = settings_used or settings_sift

            if method == "opencv":
                pano = np.asarray(pano_bgr)
                if pano.ndim == 2:
                    bands_out = 1
                else:
                    bands_out = int(pano.shape[2])
                if pano.dtype != np.uint8:
                    pano = np.clip(pano, 0, 255).astype(np.uint8, copy=False)
                if bands_out == 3:
                    # OpenCV uses BGR; libvips expects RGB channel order.
                    pano = pano[:, :, ::-1]
                pano = np.ascontiguousarray(pano)
                h, w = int(pano.shape[0]), int(pano.shape[1])

                pano_bytes = pano.tobytes()
                mosaic = pyvips.Image.new_from_memory(pano_bytes, int(w), int(h), int(bands_out), "uchar")

                meta = {
                    "method": "opencv",
                    "requested_method": str(requested_method),
                    "rows": int(nrows),
                    "cols": int(ncols),
                    "tile_w_px": int(tile_w),
                    "tile_h_px": int(tile_h),
                    "size_px": [int(w), int(h)],
                    "downsample": int(ds),
                    "settings": settings,
                    "filtered_tiles": (None if not dropped_note else dropped_note),
                }
                try:
                    with open(os.path.join(out_dir, "stitch_meta.json"), "w", encoding="utf-8") as f:
                        json.dump(meta, f, indent=2, sort_keys=True)
                except Exception:
                    pass

                # Best-quality output: do not apply lossy compression.
                comp = (tiff_compression or "none").strip().lower()
                if comp not in {"none", "lzw", "deflate"}:
                    comp = "none"

                if build_pyramidal_tiff:
                    out_path = os.path.join(out_dir, "mosaic_pyramid.tif")
                    kwargs = {
                        "tile": True,
                        "pyramid": True,
                        "bigtiff": True,
                        "tile_width": int(pyramid_tile_px),
                        "tile_height": int(pyramid_tile_px),
                        "compression": comp,
                    }
                    try:
                        mosaic.tiffsave(out_path, **kwargs)
                    except Exception as exc:
                        _append_err("tiffsave", exc)
                        raise

                if build_deepzoom:
                    dz_root = os.path.join(out_dir, "deepzoom")
                    try:
                        os.makedirs(dz_root, exist_ok=True)
                    except Exception:
                        pass
                    dz_base = os.path.join(dz_root, "mosaic")
                    try:
                        mosaic.dzsave(dz_base, tile_size=256, overlap=1, suffix=".png")
                        _write_deepzoom_viewer()
                    except Exception as exc:
                        _append_err("dzsave", exc)
                        raise

                return

        # Estimate affine mapping from bed coordinates (mm) -> pixel deltas using adjacent overlaps.
        #
        # We also collect measured neighbor shifts for a global least-squares refinement (reduces
        # per-tile drift to a few pixels when mechanics/backlash are imperfect).
        neighbor_pairs: list[tuple[tuple[int, int], tuple[int, int]]] = []
        for (r, c) in list(by_rc.keys()):
            if (r, c + 1) in by_rc:
                neighbor_pairs.append(((int(r), int(c)), (int(r), int(c + 1))))
            if (r + 1, c) in by_rc:
                neighbor_pairs.append(((int(r), int(c)), (int(r + 1), int(c))))

        orb_edges: dict[tuple[tuple[int, int], tuple[int, int]], tuple[float, float, int]] = {}
        for a_rc, b_rc in neighbor_pairs:
            a = by_rc.get(a_rc)
            b = by_rc.get(b_rc)
            if a is None or b is None:
                continue
            sh = _estimate_shift_px(str(a["path"]), str(b["path"]))
            if sh is not None:
                orb_edges[(a_rc, b_rc)] = (float(sh[0]), float(sh[1]), int(sh[2]))

        def _collect_vx_vy(
            edges_in: list[tuple[tuple[int, int], tuple[int, int], float, float, int]]
        ) -> tuple[list[np.ndarray], list[np.ndarray]]:
            vx_s: list[np.ndarray] = []
            vy_s: list[np.ndarray] = []
            for a_rc, b_rc, dx, dy, keep_n in edges_in:
                a = by_rc.get(a_rc)
                b = by_rc.get(b_rc)
                if a is None or b is None:
                    continue
                dx_mm = float(b["x_mm"]) - float(a["x_mm"])
                dy_mm = float(b["y_mm"]) - float(a["y_mm"])
                if abs(dx_mm) > 1e-6 and int(keep_n) >= 20:
                    vx_s.append(np.array([float(dx) / float(dx_mm), float(dy) / float(dx_mm)], dtype=np.float64))
                if abs(dy_mm) > 1e-6 and int(keep_n) >= 20:
                    vy_s.append(np.array([float(dx) / float(dy_mm), float(dy) / float(dy_mm)], dtype=np.float64))
            return (vx_s, vy_s)

        edges_orb: list[tuple[tuple[int, int], tuple[int, int], float, float, int]] = []
        for (a_rc, b_rc), (dx, dy, keep_n) in orb_edges.items():
            edges_orb.append((a_rc, b_rc, float(dx), float(dy), int(keep_n)))

        vx_samples, vy_samples = _collect_vx_vy(edges_orb)

        def _robust_median_vec(samples: list[np.ndarray]) -> np.ndarray:
            arr = np.stack(samples, axis=0).astype(np.float64)
            med = np.median(arr, axis=0)
            mad = np.median(np.abs(arr - med), axis=0) + 1e-6
            z = np.max(np.abs((arr - med) / mad), axis=1)
            keep = z < 5.0
            if int(keep.sum()) >= 8:
                med = np.median(arr[keep], axis=0)
            return med.astype(np.float64)

        if len(vx_samples) < 8 or len(vy_samples) < 8:
            raise RuntimeError(
                "Could not estimate stitch geometry (need more overlap/texture).\n"
                f"Got pairs: x={len(vx_samples)}, y={len(vy_samples)}"
            )

        vx = _robust_median_vec(vx_samples)  # (px/mm x, px/mm y)
        vy = _robust_median_vec(vy_samples)

        # Optional downsample of the stitched output (reserved; currently scan UI always uses 1).
        if ds > 1:
            vx = vx / float(ds)
            vy = vy / float(ds)
            tile_w = int(round(float(tile_w) / float(ds)))
            tile_h = int(round(float(tile_h) / float(ds)))

        # Choose an origin in bed space to keep numbers small.
        x0_mm = min(float(v["x_mm"]) for v in by_rc.values())
        y0_mm = min(float(v["y_mm"]) for v in by_rc.values())

        # Initial placements from the affine model.
        rc_keys = sorted(by_rc.keys(), key=lambda rc: (rc[0], rc[1]))
        idx_of: dict[tuple[int, int], int] = {rc: i for i, rc in enumerate(rc_keys)}
        ntiles = int(len(rc_keys))

        pos0 = np.zeros((ntiles, 2), dtype=np.float64)
        path_by_idx: list[str] = [""] * int(ntiles)
        for rc, i in idx_of.items():
            info = by_rc[rc]
            dxm = float(info["x_mm"]) - float(x0_mm)
            dym = float(info["y_mm"]) - float(y0_mm)
            p = (vx * float(dxm)) + (vy * float(dym))
            pos0[int(i), 0] = float(p[0])
            pos0[int(i), 1] = float(p[1])
            path_by_idx[int(i)] = str(info["path"])

        def _refine_shift_phase(
            path_a: str,
            path_b: str,
            *,
            exp_dx_full: float,
            exp_dy_full: float,
        ) -> tuple[float, float, float] | None:
            if cv2 is None:
                return None
            la = _load_gray(path_a)
            lb = _load_gray(path_b)
            if la is None or lb is None:
                return None
            a, scale_a = la
            b, scale_b = lb
            if a.shape[:2] != b.shape[:2] or abs(float(scale_a) - float(scale_b)) > 1e-6:
                return None
            scale = float(scale_a)
            if scale <= 0:
                return None

            h, w = a.shape[:2]
            dx_ds = float(exp_dx_full) * float(scale)
            dy_ds = float(exp_dy_full) * float(scale)
            dx_i = int(round(dx_ds))
            dy_i = int(round(dy_ds))

            # Define overlap such that a_roi at (x,y) corresponds to b_roi at (x+dx_i, y+dy_i).
            #
            # Here `exp_dx_full/exp_dy_full` are the expected A->B translation in pixels.
            x0 = max(0, -int(dx_i))
            y0 = max(0, -int(dy_i))
            x1 = min(int(w), int(w) - int(dx_i))
            y1 = min(int(h), int(h) - int(dy_i))
            if x1 <= x0 or y1 <= y0:
                return None
            roi_w = int(x1 - x0)
            roi_h = int(y1 - y0)
            if roi_w < 80 or roi_h < 80:
                return None

            a_roi = a[int(y0) : int(y1), int(x0) : int(x1)]
            b_roi = b[int(y0 + dy_i) : int(y1 + dy_i), int(x0 + dx_i) : int(x1 + dx_i)]
            if a_roi.shape[:2] != b_roi.shape[:2]:
                return None

            a_f = a_roi.astype(np.float32)
            b_f = b_roi.astype(np.float32)
            a_f -= float(a_f.mean())
            b_f -= float(b_f.mean())

            try:
                win = cv2.createHanningWindow((int(roi_w), int(roi_h)), cv2.CV_32F)
            except Exception:
                try:
                    wy = np.hanning(int(roi_h)).astype(np.float32)
                    wx = np.hanning(int(roi_w)).astype(np.float32)
                    win = (wy[:, None] * wx[None, :]).astype(np.float32)
                except Exception:
                    win = None

            try:
                (sx, sy), resp = cv2.phaseCorrelate(a_f, b_f, win) if win is not None else cv2.phaseCorrelate(a_f, b_f)
            except Exception:
                return None

            try:
                resp_f = float(resp)
            except Exception:
                resp_f = 0.0
            if resp_f < 0.08:
                return None

            lim = max(30.0, min(200.0, 0.25 * float(min(roi_w, roi_h))))
            if abs(float(sx)) > float(lim) or abs(float(sy)) > float(lim):
                return None

            # phaseCorrelate returns the shift to apply to src2 (b_roi) to best match src1 (a_roi),
            # i.e. it is the negative residual. Convert back to full-res residual and subtract.
            dx_full = float(exp_dx_full) - (float(sx) / float(scale))
            dy_full = float(exp_dy_full) - (float(sy) / float(scale))
            return (float(dx_full), float(dy_full), float(resp_f))

        # Neighbor translation measurements used to refine per-tile placement.
        # Prefer phase correlation in the predicted overlap (fast, robust when resp is high),
        # otherwise fall back to ORB matches.
        edges: list[tuple[tuple[int, int], tuple[int, int], float, float, float]] = []  # (a_rc,b_rc,dx,dy,conf)
        phase_min_resp = 0.12
        orb_min_inliers = 25
        if cv2 is not None:
            for a_rc, b_rc in neighbor_pairs:
                i = idx_of.get(a_rc)
                j = idx_of.get(b_rc)
                if i is None or j is None:
                    continue
                path_a = str(by_rc[a_rc]["path"])
                path_b = str(by_rc[b_rc]["path"])
                exp = pos0[int(j)] - pos0[int(i)]

                refined = _refine_shift_phase(path_a, path_b, exp_dx_full=float(exp[0]), exp_dy_full=float(exp[1]))
                if refined is not None:
                    dx_r, dy_r, resp = refined
                    if float(resp) >= float(phase_min_resp):
                        edges.append((a_rc, b_rc, float(dx_r), float(dy_r), float(max(0.0, min(1.0, float(resp))))))
                        continue

                sh = orb_edges.get((a_rc, b_rc))
                if sh is None:
                    continue
                dx_o, dy_o, keep_n = sh
                if int(keep_n) < int(orb_min_inliers):
                    continue
                conf_o = float(max(0.0, min(1.0, float(keep_n) / 80.0)))
                edges.append((a_rc, b_rc, float(dx_o), float(dy_o), float(conf_o)))
        else:
            for (a_rc, b_rc), (dx_o, dy_o, keep_n) in orb_edges.items():
                if int(keep_n) < int(orb_min_inliers):
                    continue
                conf_o = float(max(0.0, min(1.0, float(keep_n) / 80.0)))
                edges.append((a_rc, b_rc, float(dx_o), float(dy_o), float(conf_o)))

        pairs_x_used = int(len(vx_samples))
        pairs_y_used = int(len(vy_samples))

        # Global refinement: solve small per-tile corrections using measured neighbor translations.
        pos = pos0.copy()
        used_edges: list[tuple[int, int, float, float, float]] = []  # (i,j,dx_resid,dy_resid,w)
        max_resid = max(25.0, min(180.0, 0.12 * float(min(tile_w, tile_h))))
        min_conf = 0.15
        for a_rc, b_rc, dx, dy, conf in edges:
            if float(conf) < float(min_conf):
                continue
            i = idx_of.get(a_rc)
            j = idx_of.get(b_rc)
            if i is None or j is None:
                continue
            meas = np.array([float(dx), float(dy)], dtype=np.float64)
            exp = pos0[int(j)] - pos0[int(i)]
            resid = meas - exp
            resid_norm = float(np.hypot(float(resid[0]), float(resid[1])))
            if resid_norm > float(max_resid):
                continue
            w = max(0.2, min(5.0, float(conf) * 5.0))
            used_edges.append((int(i), int(j), float(resid[0]), float(resid[1]), float(w)))

        rms_after: float | None = None
        used_edges_final = used_edges
        refine_min_edges = max(8, int(0.25 * float(ntiles)))
        if len(used_edges) >= int(refine_min_edges):
            try:
                try:
                    from scipy.sparse import coo_matrix  # type: ignore
                    from scipy.sparse.linalg import lsqr  # type: ignore

                    use_sparse = True
                except Exception:
                    use_sparse = False

                def _solve(edge_list: list[tuple[int, int, float, float, float]]) -> np.ndarray:
                    rows: list[int] = []
                    cols: list[int] = []
                    data: list[float] = []
                    bx: list[float] = []
                    by: list[float] = []
                    rr = 0
                    for i2, j2, rx, ry, w2 in edge_list:
                        rows.extend([rr, rr])
                        cols.extend([int(i2), int(j2)])
                        data.extend([-float(w2), float(w2)])
                        bx.append(float(w2) * float(rx))
                        by.append(float(w2) * float(ry))
                        rr += 1

                    # Anchor delta for the first tile to 0 (fix translation gauge freedom).
                    anchor_w = 10.0
                    rows.append(rr)
                    cols.append(0)
                    data.append(float(anchor_w))
                    bx.append(0.0)
                    by.append(0.0)
                    rr += 1

                    # Regularize corrections toward 0 to prevent global drift when matches are weak.
                    prior_w = 0.25
                    for k in range(int(ntiles)):
                        rows.append(rr)
                        cols.append(int(k))
                        data.append(float(prior_w))
                        bx.append(0.0)
                        by.append(0.0)
                        rr += 1

                    if use_sparse:
                        A = coo_matrix((data, (rows, cols)), shape=(int(rr), int(ntiles))).tocsr()
                        dx_sol = lsqr(A, np.array(bx, dtype=np.float64), atol=1e-6, btol=1e-6, iter_lim=2000)[0]
                        dy_sol = lsqr(A, np.array(by, dtype=np.float64), atol=1e-6, btol=1e-6, iter_lim=2000)[0]
                    else:
                        # Dense fallback (ok for small grids).
                        A = np.zeros((int(rr), int(ntiles)), dtype=np.float64)
                        for v, r_i, c_i in zip(data, rows, cols, strict=False):
                            A[int(r_i), int(c_i)] += float(v)
                        dx_sol, *_ = np.linalg.lstsq(A, np.array(bx, dtype=np.float64), rcond=None)
                        dy_sol, *_ = np.linalg.lstsq(A, np.array(by, dtype=np.float64), rcond=None)

                    return np.stack([dx_sol, dy_sol], axis=1).astype(np.float64)

                # 1-2 iterations of outlier pruning.
                edge_list = list(used_edges)
                prune_min_edges = max(8, int(0.15 * float(ntiles)))
                for _iter in range(2):
                    if len(edge_list) < int(prune_min_edges):
                        break
                    delta_tmp = _solve(edge_list)
                    err_norms: list[float] = []
                    for i2, j2, rx, ry, _w2 in edge_list:
                        e = (delta_tmp[int(j2)] - delta_tmp[int(i2)]) - np.array([float(rx), float(ry)], dtype=np.float64)
                        err_norms.append(float(np.hypot(float(e[0]), float(e[1]))))
                    if not err_norms:
                        break
                    err_arr = np.array(err_norms, dtype=np.float64)
                    med = float(np.median(err_arr))
                    mad = float(np.median(np.abs(err_arr - float(med)))) + 1e-6
                    lim = float(max(20.0, min(90.0, float(med) + (4.0 * float(mad)))))
                    new_edges = [e for e, en in zip(edge_list, err_norms, strict=False) if float(en) <= float(lim)]
                    if len(new_edges) == len(edge_list):
                        break
                    if len(new_edges) < int(prune_min_edges):
                        break
                    edge_list = new_edges

                used_edges_final = edge_list
                delta = _solve(used_edges_final)
                pos = (pos0 + delta).astype(np.float64)

                if used_edges_final:
                    err_norms = []
                    for i2, j2, rx, ry, _w2 in used_edges_final:
                        e = (delta[int(j2)] - delta[int(i2)]) - np.array([float(rx), float(ry)], dtype=np.float64)
                        err_norms.append(float(np.hypot(float(e[0]), float(e[1]))))
                    err_arr = np.array(err_norms, dtype=np.float64)
                    rms_after = float(np.sqrt(float(np.mean(np.square(err_arr)))))
            except Exception:
                rms_after = None
                used_edges_final = used_edges

        used_edges = used_edges_final

        placements: list[tuple[float, float, str]] = []
        min_x = None
        min_y = None
        max_x = None
        max_y = None
        for rc in rc_keys:
            i = idx_of[rc]
            x_px = float(pos[int(i), 0])
            y_px = float(pos[int(i), 1])
            path = str(path_by_idx[int(i)])
            placements.append((x_px, y_px, path))

            x2 = x_px + float(tile_w)
            y2 = y_px + float(tile_h)
            min_x = x_px if min_x is None else min(min_x, x_px)
            min_y = y_px if min_y is None else min(min_y, y_px)
            max_x = x2 if max_x is None else max(max_x, x2)
            max_y = y2 if max_y is None else max(max_y, y2)

        if min_x is None or min_y is None or max_x is None or max_y is None:
            raise RuntimeError("No tiles to stitch.")

        width = int(max(1, math.ceil(float(max_x - min_x))))
        height = int(max(1, math.ceil(float(max_y - min_y))))

        mosaic = pyvips.Image.black(int(width), int(height), bands=bands)

        for x_px, y_px, path in placements:
            x = int(round(float(x_px) - float(min_x)))
            y = int(round(float(y_px) - float(min_y)))
            if x < 0:
                x = 0
            if y < 0:
                y = 0
            tile = pyvips.Image.new_from_file(path, access="random")
            if ds > 1:
                tile = tile.resize(1.0 / float(ds), kernel="lanczos3")
            if int(tile.bands) != bands:
                tile = tile.extract_band(0, n=bands)
            mosaic = mosaic.insert(tile, int(x), int(y), expand=False)

        meta = {
            "method": "bed",
            "requested_method": str(requested_method),
            "rows": int(nrows),
            "cols": int(ncols),
            "tile_w_px": int(tile_w),
            "tile_h_px": int(tile_h),
            "vx_px_per_mm": [float(vx[0]), float(vx[1])],
            "vy_px_per_mm": [float(vy[0]), float(vy[1])],
            "origin_mm": [float(x0_mm), float(y0_mm)],
            "size_px": [int(width), int(height)],
            "pairs_used": {"x": int(pairs_x_used), "y": int(pairs_y_used)},
            "edges_used": int(len(used_edges)),
            "edge_rms_px": (None if rms_after is None else float(rms_after)),
            "downsample": int(ds),
        }
        try:
            with open(os.path.join(out_dir, "stitch_meta.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, sort_keys=True)
        except Exception:
            pass

        # Best-quality output: do not apply lossy compression.
        comp = (tiff_compression or "none").strip().lower()
        if comp not in {"none", "lzw", "deflate"}:
            comp = "none"

        def _write_err(stage: str, exc: Exception) -> None:
            try:
                with open(os.path.join(out_dir, "stitch_error.txt"), "a", encoding="utf-8") as f:
                    f.write(f"[{stage}] {exc}\\n")
                    f.write(traceback.format_exc())
                    f.write("\n\n")
            except Exception:
                pass

        if build_pyramidal_tiff:
            out_path = os.path.join(out_dir, "mosaic_pyramid.tif")
            kwargs = {
                "tile": True,
                "pyramid": True,
                "bigtiff": True,
                "tile_width": int(pyramid_tile_px),
                "tile_height": int(pyramid_tile_px),
                "compression": comp,
            }
            try:
                mosaic.tiffsave(out_path, **kwargs)
            except Exception as exc:
                _write_err("tiffsave", exc)
                raise

        if build_deepzoom:
            dz_root = os.path.join(out_dir, "deepzoom")
            try:
                os.makedirs(dz_root, exist_ok=True)
            except Exception:
                pass
            dz_base = os.path.join(dz_root, "mosaic")
            try:
                mosaic.dzsave(dz_base, tile_size=256, overlap=1, suffix=".png")
                _write_deepzoom_viewer()
            except Exception as exc:
                _write_err("dzsave", exc)
                raise
