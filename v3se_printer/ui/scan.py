from __future__ import annotations

import json
import os
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ..scan.params import ScanParams, fmt_duration as _fmt_duration


class ScanTabMixin:
    def _build_scan_tab(self, parent: ttk.Frame) -> None:
        intro = ttk.LabelFrame(parent, text="Camera Bed Scan (tiles → stitched output)", padding=10)
        intro.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(
            intro,
            text=(
                "Captures full-resolution tiles across the work area. Optional per-tile autofocus.\n"
                "Tiles are saved as TIFF (lossless; compression configurable). Optional stitching uses a layout-based affine stitcher.\n"
                "Output: stitched TIFF (mosaic_full.tif; full-res; DPI/PPI metadata set from step size) and a JPEG preview (mosaic_thumb_2000.jpg)."
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

        ttk.Checkbutton(stitch, text="Keep stitched TIFF (mosaic_full.tif)", variable=self._scan_build_pyramid_var).grid(
            row=0, column=0, columnspan=6, sticky=tk.W
        )

        ttk.Label(stitch, text="Stitching:").grid(row=2, column=0, sticky=tk.W, pady=(8, 0))
        ttk.Label(stitch, text="Affine stitcher (auto)").grid(
            row=2, column=1, columnspan=5, sticky=tk.W, padx=(6, 0), pady=(8, 0)
        )

        out = ttk.LabelFrame(parent, text="Output", padding=10)
        out.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))

        ttk.Label(out, text="Folder:").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(out, textvariable=self._scan_out_dir_var, width=60).grid(row=0, column=1, sticky=tk.W, padx=(6, 10))
        ttk.Button(out, text="Choose…", command=self._scan_choose_dir).grid(row=0, column=2, sticky=tk.W)

        ttk.Label(out, text="TIFF compression:").grid(row=1, column=0, sticky=tk.W, pady=(8, 0))
        ttk.Combobox(
            out,
            textvariable=self._scan_tiff_compression_var,
            values=["none", "lzw", "deflate"],
            width=10,
            state="readonly",
        ).grid(row=1, column=1, sticky=tk.W, padx=(6, 10), pady=(8, 0))
        ttk.Label(out, text="(reduces disk usage)").grid(row=1, column=2, sticky=tk.W, pady=(8, 0))
        out.grid_columnconfigure(1, weight=1)

        btns = ttk.Frame(parent)
        btns.pack(side=tk.TOP, fill=tk.X, pady=(12, 0))

        self.scan_start_btn = ttk.Button(btns, text="Start Scan", command=self._scan_start)
        self.scan_start_btn.pack(side=tk.LEFT)
        self.scan_stop_btn = ttk.Button(btns, text="Stop", command=self._scan_stop_clicked, state="disabled")
        self.scan_stop_btn.pack(side=tk.LEFT, padx=(10, 0))

        ttk.Label(btns, textvariable=self._scan_status_var).pack(side=tk.LEFT, padx=(12, 0))

        prog = ttk.Frame(parent)
        prog.pack(side=tk.TOP, fill=tk.X, pady=(8, 0))
        ttk.Label(prog, textvariable=self._scan_stitch_progress_text_var, width=28).pack(side=tk.LEFT)
        ttk.Progressbar(
            prog,
            variable=self._scan_stitch_progress_var,
            maximum=100.0,
            mode="determinate",
            length=260,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))

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
            capture_settle_ms = max(0, min(5000, int(capture_settle_ms)))
            downsample = 1
            try:
                tiff_comp = str(self._scan_tiff_compression_var.get()).strip().lower() or "none"
            except Exception:
                tiff_comp = "none"
            if tiff_comp not in {"none", "lzw", "deflate"}:
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
                capture_settle_ms=int(capture_settle_ms),
                downsample=int(downsample),
                build_pyramidal_tiff=bool(self._scan_build_pyramid_var.get()),
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
            self._scan_capture_settle_ms_var,
            self._scan_build_pyramid_var,
            self._scan_tiff_compression_var,
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
        capture_settle_ms = max(0, min(5000, int(capture_settle_ms)))
        downsample = 1
        try:
            tiff_comp = str(self._scan_tiff_compression_var.get()).strip().lower() or "none"
        except Exception:
            tiff_comp = "none"
        if tiff_comp not in {"none", "lzw", "deflate"}:
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
            capture_settle_ms=int(capture_settle_ms),
            downsample=int(downsample),
            build_pyramidal_tiff=bool(self._scan_build_pyramid_var.get()),
            tiff_compression=str(tiff_comp),
            out_base_dir=str(out_base),
        )

        est_s = self._scan_estimate_seconds(params)
        self._scan_estimate_var.set(f"Estimate: { _fmt_duration(est_s) } (capture only; stitching extra)")

        if bool(getattr(self, "_confirm_motion_var", tk.BooleanVar(value=False)).get()):
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
        try:
            self._scan_stitch_progress_var.set(0.0)
            self._scan_stitch_progress_text_var.set("Stitching: idle")
        except Exception:
            pass

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
        from ..scan.worker import run_scan_worker

        run_scan_worker(self, params)
