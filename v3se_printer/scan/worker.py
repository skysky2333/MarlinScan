from __future__ import annotations

import json
import math
import os
import tempfile
import time

from ..progress import StepProgressTracker, format_step_progress
from ..uvc import compute_sharpness, transform_frame
from .io import imwrite, is_no_space_error
from .params import ScanParams, fmt_duration as _fmt_duration
from .stitch_outputs import stitch_scan_outputs


def _write_json(path: str, payload: object) -> None:
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def run_scan_worker(gui: object, params: ScanParams) -> None:
    stitch_progress_tracker = StepProgressTracker()

    def ev(kind: str, payload: object) -> None:
        try:
            getattr(gui, "_events").put((kind, payload))
        except Exception:
            pass

    def status(msg: str) -> None:
        ev("scan-status", msg)

    def stitch_progress(phase: str, label: str, completed: int, total: int | None, unit: str) -> None:
        current = stitch_progress_tracker.update(phase, label, completed, total, unit)
        percent = 100.0 if total == 0 else 0.0 if total is None else completed / total * 100.0
        ev("scan-stitch-progress", (percent, format_step_progress(current)))

    def finish(ok: bool, msg: str) -> None:
        ev("scan-finished", (bool(ok), str(msg)))

    try:
        x0 = float(params.x_min)
        x1 = float(params.x_max)
        y0 = float(params.y_min)
        y1 = float(params.y_max)
        step_x = float(params.step_x_mm)
        step_y = float(params.step_y_mm)
    except (TypeError, ValueError):
        finish(False, "Invalid scan parameters: bounds and steps must be numbers.")
        return
    if not all(math.isfinite(value) for value in (x0, x1, y0, y1, step_x, step_y)):
        finish(False, "Invalid scan parameters: bounds and steps must be finite.")
        return
    if x1 < x0 or y1 < y0:
        finish(False, "Invalid scan parameters: minimum bounds must not exceed maximum bounds.")
        return
    if step_x <= 0 or step_y <= 0:
        finish(False, "Invalid scan parameters: scan steps must be positive.")
        return

    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception as exc:
        finish(False, f"Missing dependency: {exc}")
        return
    try:
        if hasattr(cv2, "ocl") and hasattr(cv2.ocl, "setUseOpenCL"):
            cv2.ocl.setUseOpenCL(False)
    except Exception:
        pass

    if getattr(gui, "_worker", None) is None:
        finish(False, "Printer disconnected.")
        return
    if not getattr(gui, "_cam_connected", False):
        finish(False, "Camera disconnected.")
        return

    ts = time.strftime("%Y%m%d_%H%M%S")
    out_base_dir = os.path.expanduser(params.out_base_dir)
    try:
        os.makedirs(out_base_dir, exist_ok=True)
        out_dir = tempfile.mkdtemp(prefix=f"scan_{ts}_", dir=out_base_dir)
    except Exception as exc:
        finish(False, f"Failed to create output dir: {exc}")
        return

    try:
        _write_json(os.path.join(out_dir, "scan_params.json"), {**params.__dict__, "image_roles": "single"})
    except Exception as exc:
        finish(False, f"Scan failed: could not write scan_params.json: {exc}")
        return

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
        restore_coord = str(getattr(gui, "_coord_mode_var").get())
    except Exception:
        restore_coord = "absolute"

    try:
        speed_xy = float(getattr(gui, "_speed_xy_var").get())
    except Exception:
        speed_xy = 150.0
    speed_xy = max(5.0, min(500.0, speed_xy))
    feed_xy = int(round(speed_xy * 60.0))
    try:
        speed_z = float(getattr(gui, "_speed_z_var").get())
    except Exception:
        speed_z = 10.0
    speed_z = max(0.5, float(speed_z))
    feed_z = int(round(speed_z * 60.0))

    try:
        z_min = float(getattr(gui, "_bed_z_min_var").get())
        z_max = float(getattr(gui, "_bed_z_max_var").get())
    except Exception:
        z_min = 0.0
        z_max = 250.0
    if z_max < z_min:
        z_min, z_max = z_max, z_min

    def gcode_wait(cmd: str, *, timeout_s: float, tag_prefix: str) -> None:
        ok, _lines = getattr(gui, "_send_and_wait")(cmd, timeout_s=float(timeout_s), tag_prefix=tag_prefix, log=False)
        if not ok:
            raise RuntimeError(f"G-code failed: {cmd}")

    try:
        gcode_wait("G90", timeout_s=5.0, tag_prefix="scan_g90")
    except Exception as exc:
        finish(False, f"Scan failed: {exc}")
        return

    def restore_scan_state() -> None:
        try:
            if restore_coord == "relative":
                gcode_wait("G91", timeout_s=3.0, tag_prefix="scan_restore_g91")
            else:
                gcode_wait("G90", timeout_s=3.0, tag_prefix="scan_restore_g90")
        except Exception:
            pass
        try:
            getattr(gui, "_cam_af_stop").clear()
        except Exception:
            pass

    def require_not_stopped() -> None:
        if getattr(gui, "_scan_stop").is_set():
            raise InterruptedError("stopped")

    with getattr(gui, "_cam_frame_cond"):
        last_seq = int(getattr(gui, "_cam_frame_seq"))

    try:
        cam_fps = float(getattr(getattr(gui, "_cam_config"), "fps"))  # type: ignore[attr-defined]
    except Exception:
        cam_fps = 30.0
    cam_fps = max(1.0, float(cam_fps))
    # After motion, UVC pipelines can deliver a few buffered frames that were captured during movement.
    # Drop a short burst before using frames for AF/capture.
    warmup_frames = max(2, min(8, int(round(cam_fps * 0.12))))
    try:
        cfg_settle_s = float(getattr(getattr(gui, "_cam_config"), "af_settle_ms")) / 1000.0  # type: ignore[attr-defined]
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
            if getattr(gui, "_scan_stop").is_set():
                break
            last_seq, _fr = getattr(gui, "_camera_wait_for_next_frame")(last_seq, timeout_s=float(timeout_s))

    def capture_frames(n: int) -> list[object]:
        nonlocal last_seq
        out: list[object] = []
        need = max(1, int(n))
        flush_frames(int(warmup_frames), timeout_s=1.2)
        if float(motion_settle_s) > 1e-6:
            time.sleep(float(motion_settle_s))
        flush_frames(1, timeout_s=1.2)
        for _i in range(need):
            if getattr(gui, "_scan_stop").is_set():
                break
            last_seq, fr = getattr(gui, "_camera_wait_for_next_frame")(last_seq, timeout_s=2.5)
            if fr is None:
                continue
            try:
                out.append(fr.copy())  # type: ignore[union-attr]
            except Exception:
                out.append(fr)
        return out

    def align_frames(frames: list["np.ndarray"], *, ds: int) -> list["np.ndarray"]:
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

    def stack(frames: list["np.ndarray"], *, mode: str, ds: int) -> "np.ndarray | None":
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

    try:
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
                    require_not_stopped()
                    xx = float(mesh_xs[int(i)])
                    status(f"Scan: focus mesh ({j+1}/{my}) ({ii+1}/{mx})  X={xx:.2f} Y={yy:.2f}")
                    gcode_wait(f"G0 X{xx:g} Y{yy:g} F{feed_xy}", timeout_s=12.0, tag_prefix="scan_cal_xy")
                    gcode_wait("M400", timeout_s=300.0, tag_prefix="scan_cal_m400_xy")
                    if float(motion_settle_s) > 1e-6:
                        time.sleep(float(motion_settle_s))
                    flush_frames(int(warmup_frames), timeout_s=1.2)

                    try:
                        getattr(gui, "_cam_af_stop").clear()
                    except Exception:
                        pass

                    # For calibration, use the same autofocus algorithm as the manual "Auto Focus" button.
                    ok_af, z_af, _f, _msg = getattr(gui, "_camera_autofocus_thread")(
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
                        ok_af, z_af, _f, _msg = getattr(gui, "_camera_autofocus_thread")(
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
                    _write_json(
                        os.path.join(out_dir, "focus_mesh.json"),
                        {
                            "mesh_nx": int(mx),
                            "mesh_ny": int(my),
                            "x_mm": [float(v) for v in mesh_xs],
                            "y_mm": [float(v) for v in mesh_ys],
                            "z_mm": [[float(v) for v in row] for row in mesh_z],
                            "plane_fallback": {"a": float(a), "b": float(b2), "c": float(c)},
                        },
                    )
                except Exception as exc:
                    raise RuntimeError(f"could not write focus_mesh.json: {exc}") from exc
                status(f"Scan: focus mesh ready ({mx}×{my})")
            else:
                status("Scan: focus mesh calibration failed (need ≥3 good points); continuing without mesh")

        for r, y in enumerate(ys):
            if getattr(gui, "_scan_stop").is_set():
                raise InterruptedError("stopped")
            row_xs = list(xs)
            if params.serpentine and (r % 2 == 1):
                row_xs.reverse()
            for c_idx, x in enumerate(row_xs):
                if getattr(gui, "_scan_stop").is_set():
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
                    if getattr(gui, "_scan_stop").is_set():
                        raise InterruptedError("stopped")
                    try:
                        getattr(gui, "_cam_af_stop").clear()
                    except Exception:
                        pass

                    # Quick local refine around the predicted/last focus Z, using the same autofocus
                    # algorithm as the manual button but in a small Z window.
                    z_center = float(z_hint) if (z_hint is not None) else None
                    try:
                        slow_step_cfg = float(getattr(getattr(gui, "_cam_config"), "af_slow_step_mm"))  # type: ignore[attr-defined]
                    except Exception:
                        slow_step_cfg = 0.1
                    tile_span = max(0.18, min(0.6, max(0.25, 4.0 * float(slow_step_cfg))))

                    if z_center is None:
                        ok_af, _z, _f, _msg = getattr(gui, "_camera_autofocus_thread")(
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
                        ok_af, _z, _f, _msg = getattr(gui, "_camera_autofocus_thread")(
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

                cfg = getattr(gui, "_cam_config", None)
                tf: list["np.ndarray"] = []
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
                    # Save lossless TIFF. Compression is configurable to avoid huge disk usage.
                    params_write: list[int] = []
                    if hasattr(cv2, "IMWRITE_TIFF_COMPRESSION"):
                        comp = (str(getattr(params, "tiff_compression", "none")) or "none").strip().lower()
                        code = 1  # none
                        if comp == "lzw":
                            code = 5
                        elif comp == "deflate":
                            code = 8
                        params_write = [int(cv2.IMWRITE_TIFF_COMPRESSION), int(code)]
                    imwrite(cv2, path, tile, params_write)
                except Exception as exc:
                    status(f"Scan: failed to write tile: {exc}")
                    if is_no_space_error(exc):
                        raise RuntimeError(
                            "No space left on device while writing tiles. Free disk space or choose a different output folder."
                        ) from exc
                    continue

                tiles.append({"row": int(r), "col": int(col), "x_mm": float(x), "y_mm": float(y), "file": filename})

        try:
            _write_json(os.path.join(out_dir, "tiles.json"), tiles)
        except Exception as exc:
            raise RuntimeError(f"could not write tiles.json: {exc}") from exc

        if bool(params.build_pyramidal_tiff) and tiles:
            stitch_ok = True
            status("Scan: stitching (this can take a while)…")
            try:
                require_not_stopped()
                stitch_scan_outputs(
                    tiles=tiles,
                    out_dir=out_dir,
                    build_pyramidal_tiff=bool(params.build_pyramidal_tiff),
                    tiff_compression=str(params.tiff_compression),
                    image_roles="single",
                    progress_cb=stitch_progress,
                    cancel_cb=require_not_stopped,
                )
                require_not_stopped()
            except InterruptedError:
                raise
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

        require_not_stopped()
        if bool(params.build_pyramidal_tiff) and tiles and not stitch_ok:
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
        restore_scan_state()
