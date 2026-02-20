from __future__ import annotations

import json
import math
import os
import shutil
import time
import traceback
from dataclasses import dataclass
from typing import Callable

from .io import imwrite, is_no_space_error


def _tiff_imwrite_params(cv2: object, *, compression: str) -> list[int]:
    if not hasattr(cv2, "IMWRITE_TIFF_COMPRESSION"):
        return []
    comp = (compression or "none").strip().lower()
    code = 1  # none
    if comp == "lzw":
        code = 5
    elif comp == "deflate":
        code = 8
    return [int(getattr(cv2, "IMWRITE_TIFF_COMPRESSION")), int(code)]


@dataclass(frozen=True)
class _Node:
    image_path: str
    mask_path: str | None
    corner: tuple[int, int]  # global (x,y) in output pixels


def stitch_scan_outputs(
    *,
    tiles: list[dict[str, object]],
    out_dir: str,
    build_pyramidal_tiff: bool,
    build_deepzoom: bool,
    tiff_compression: str,
    deepzoom_tile_px: int,
    deepzoom_format: str,
    deepzoom_jpeg_quality: int,
    progress_cb: Callable[[float, str], None] | None = None,
) -> None:
    """
    ImageStitch-style 2×2 pyramid stitcher.

    - Estimate tile-to-tile translations via OpenCV phase correlation on downsampled grayscale
    - Solve global tile positions (least squares over neighbor edges)
    - Compose in a 2×2 pyramid using integer offsets + overwrite (keeps sharpness)
    - Carry a validity mask so padded areas never overwrite real pixels
    """
    if not bool(build_pyramidal_tiff) and not bool(build_deepzoom):
        return

    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"Missing dependency: {exc}") from exc

    try:
        if hasattr(cv2, "ocl") and hasattr(cv2.ocl, "setUseOpenCL"):
            cv2.ocl.setUseOpenCL(False)
    except Exception:
        pass

    dz_tile_px = max(128, min(4096, int(deepzoom_tile_px)))
    dz_fmt = (deepzoom_format or "jpg").strip().lower()
    if dz_fmt == "jpeg":
        dz_fmt = "jpg"
    if dz_fmt not in {"jpg", "png"}:
        dz_fmt = "jpg"
    dz_jpeg_q = max(1, min(100, int(deepzoom_jpeg_quality)))

    last_emit_ts = 0.0
    last_emit_pct = -1.0

    def _emit_progress(pct: float, msg: str) -> None:
        nonlocal last_emit_ts, last_emit_pct
        if progress_cb is None:
            return
        pct_f = max(0.0, min(100.0, float(pct)))
        now = time.monotonic()
        if pct_f in {0.0, 100.0} or (pct_f - float(last_emit_pct)) >= 0.5 or (now - float(last_emit_ts)) >= 0.5:
            last_emit_pct = float(pct_f)
            last_emit_ts = float(now)
            try:
                progress_cb(float(pct_f), str(msg))
            except Exception:
                pass

    def _write_err(stage: str, exc: Exception) -> None:
        try:
            with open(os.path.join(out_dir, "stitch_error.txt"), "a", encoding="utf-8") as f:
                f.write(f"[{stage}] {exc}\n")
                f.write(traceback.format_exc())
                f.write("\n\n")
        except Exception:
            pass

    def _cleanup_dir(path: str) -> None:
        if os.path.isdir(path):
            try:
                shutil.rmtree(path, ignore_errors=True)
            except Exception:
                pass

    def _dz_write_from_array(
        img: "np.ndarray",
        *,
        dz_base: str,
        tile_size: int,
        fmt: str,
        jpeg_quality: int,
        png_compression: int = 3,
        progress: Callable[[int], None] | None = None,
    ) -> int:
        h0, w0 = img.shape[:2]
        w0 = int(w0)
        h0 = int(h0)
        if w0 <= 0 or h0 <= 0:
            raise RuntimeError("Invalid image size for DeepZoom.")
        max_dim = max(w0, h0)
        max_level = int(math.ceil(math.log(max_dim, 2))) if max_dim > 1 else 0

        dzi_path = dz_base + ".dzi"
        files_root = dz_base + "_files"
        os.makedirs(files_root, exist_ok=True)

        fmt_norm = (fmt or "jpg").strip().lower()
        if fmt_norm == "jpeg":
            fmt_norm = "jpg"
        if fmt_norm not in {"jpg", "png"}:
            fmt_norm = "jpg"

        dzi = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Image TileSize="{ts}" Overlap="0" Format="{fmt}" '
            'xmlns="http://schemas.microsoft.com/deepzoom/2008">\n'
            '  <Size Width="{w}" Height="{h}"/>\n'
            "</Image>\n"
        ).format(ts=int(tile_size), fmt=str(fmt_norm), w=int(w0), h=int(h0))
        with open(dzi_path, "w", encoding="utf-8") as f:
            f.write(dzi)

        total_tiles = 0
        cur = img
        for level in range(int(max_level), -1, -1):
            ch, cw = cur.shape[:2]
            level_dir = os.path.join(files_root, str(level))
            os.makedirs(level_dir, exist_ok=True)
            tiles_x = int((int(cw) + int(tile_size) - 1) // int(tile_size))
            tiles_y = int((int(ch) + int(tile_size) - 1) // int(tile_size))
            if fmt_norm == "jpg" and hasattr(cv2, "IMWRITE_JPEG_QUALITY"):
                params_write = [int(cv2.IMWRITE_JPEG_QUALITY), max(1, min(100, int(jpeg_quality)))]
            elif fmt_norm == "png" and hasattr(cv2, "IMWRITE_PNG_COMPRESSION"):
                params_write = [int(cv2.IMWRITE_PNG_COMPRESSION), max(0, min(9, int(png_compression)))]
            else:
                params_write = []
            for ty in range(int(tiles_y)):
                y0 = int(ty) * int(tile_size)
                y1 = min(int(ch), int(y0) + int(tile_size))
                for tx in range(int(tiles_x)):
                    x0 = int(tx) * int(tile_size)
                    x1 = min(int(cw), int(x0) + int(tile_size))
                    tile = cur[int(y0) : int(y1), int(x0) : int(x1)]
                    out_path = os.path.join(level_dir, f"{tx}_{ty}.{fmt_norm}")
                    imwrite(cv2, out_path, tile, params_write)
                    total_tiles += 1
                    if progress is not None:
                        progress(1)
            if level > 0:
                new_w = max(1, (int(cw) + 1) // 2)
                new_h = max(1, (int(ch) + 1) // 2)
                cur = cv2.resize(cur, (int(new_w), int(new_h)), interpolation=cv2.INTER_AREA)
        return int(total_tiles)

    # Clear stale outputs.
    try:
        for fn in ("stitch_error.txt", "stitch_meta.json", "mosaic_full.tif", "deepzoom_viewer.html"):
            p = os.path.join(out_dir, fn)
            if os.path.exists(p):
                os.remove(p)
    except Exception:
        pass
    dz_root = os.path.join(out_dir, "deepzoom")
    _cleanup_dir(dz_root)
    pyramid_dir = os.path.join(out_dir, "imagestitch_pyramid")
    _cleanup_dir(pyramid_dir)

    # Parse tiles.
    by_rc: dict[tuple[int, int], str] = {}
    max_r = -1
    max_c = -1
    for t in tiles:
        try:
            r = int(t["row"])
            c = int(t["col"])
            fn = str(t["file"])
        except Exception:
            continue
        by_rc[(int(r), int(c))] = os.path.join(out_dir, str(fn))
        max_r = max(int(max_r), int(r))
        max_c = max(int(max_c), int(c))

    if not by_rc:
        return
    nrows = int(max_r) + 1
    ncols = int(max_c) + 1
    if nrows <= 0 or ncols <= 0:
        return

    any_path = next(iter(by_rc.values()))
    im0 = cv2.imread(str(any_path), cv2.IMREAD_COLOR)
    if im0 is None:
        raise RuntimeError("Failed to read a tile image.")
    tile_h, tile_w = im0.shape[:2]
    if int(tile_w) <= 0 or int(tile_h) <= 0:
        raise RuntimeError("Invalid tile size.")

    tiff_params = _tiff_imwrite_params(cv2, compression=str(tiff_compression))

    # --- Global alignment: phase correlation on downsampled grayscale ---
    align_megapix = 0.6
    phase_resp_thresh = 0.15

    orig_mp = (float(tile_w) * float(tile_h)) / 1_000_000.0
    scale_align = 1.0 if orig_mp <= 1e-9 else math.sqrt(float(align_megapix) / float(orig_mp))
    scale_align = max(0.05, min(1.0, float(scale_align)))
    w_a = max(64, int(round(float(tile_w) * float(scale_align))))
    h_a = max(64, int(round(float(tile_h) * float(scale_align))))

    gray_cache: dict[str, "np.ndarray | None"] = {}
    hanning: "np.ndarray | None" = None

    def _read_gray_small(path: str) -> "np.ndarray | None":
        if path in gray_cache:
            return gray_cache[path]
        g0 = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if g0 is None:
            gray_cache[path] = None
            return None
        if int(g0.shape[1]) != int(w_a) or int(g0.shape[0]) != int(h_a):
            g0 = cv2.resize(g0, (int(w_a), int(h_a)), interpolation=cv2.INTER_AREA)
        g = g0.astype(np.float32)
        try:
            g = g - cv2.GaussianBlur(g, (0, 0), sigmaX=3.0, sigmaY=3.0)
        except Exception:
            pass
        gray_cache[path] = g
        return g

    def _phase_shift(p1: str, p2: str) -> tuple[float, float, float] | None:
        nonlocal hanning
        a = _read_gray_small(p1)
        b = _read_gray_small(p2)
        if a is None or b is None or a.shape != b.shape:
            return None
        if hanning is None or hanning.shape != a.shape:
            try:
                hanning = cv2.createHanningWindow((int(a.shape[1]), int(a.shape[0])), cv2.CV_32F)
            except Exception:
                hanning = None
        try:
            if hanning is not None:
                (sx, sy), resp = cv2.phaseCorrelate(a, b, hanning)
            else:
                (sx, sy), resp = cv2.phaseCorrelate(a, b)
        except Exception:
            return None

        w = int(a.shape[1])
        h = int(a.shape[0])
        if float(sx) > (float(w) / 2.0):
            sx = float(sx) - float(w)
        if float(sx) < (-float(w) / 2.0):
            sx = float(sx) + float(w)
        if float(sy) > (float(h) / 2.0):
            sy = float(sy) - float(h)
        if float(sy) < (-float(h) / 2.0):
            sy = float(sy) + float(h)

        dx = float(sx) / float(scale_align)
        dy = float(sy) / float(scale_align)
        return (float(dx), float(dy), float(resp))

    def _solve_global_positions() -> dict[tuple[int, int], tuple[float, float]]:
        keys = sorted(by_rc.keys())
        idx_of: dict[tuple[int, int], int] = {k: i for i, k in enumerate(keys)}
        n = int(len(keys))

        h_edges: list[tuple[tuple[int, int], tuple[int, int]]] = []
        v_edges: list[tuple[tuple[int, int], tuple[int, int]]] = []
        for (r, c) in keys:
            if (int(r), int(c + 1)) in by_rc:
                h_edges.append(((int(r), int(c)), (int(r), int(c + 1))))
            if (int(r + 1), int(c)) in by_rc:
                v_edges.append(((int(r), int(c)), (int(r + 1), int(c))))

        total_pairs = max(1, int(len(h_edges) + len(v_edges)))
        done_pairs = 0

        measurements: list[tuple[int, int, float, float, float]] = []
        h_shifts: list[tuple[float, float]] = []
        v_shifts: list[tuple[float, float]] = []

        def _keep(dx: float, dy: float, resp: float) -> bool:
            if float(resp) < float(phase_resp_thresh):
                return False
            if abs(float(dx)) > (float(tile_w) * 0.98):
                return False
            if abs(float(dy)) > (float(tile_h) * 0.98):
                return False
            return True

        for (a_rc, b_rc) in h_edges:
            done_pairs += 1
            if done_pairs % 20 == 0:
                _emit_progress(0.5 + (4.0 * (float(done_pairs) / float(total_pairs))), "Stitching: estimating positions…")
            v = _phase_shift(by_rc[a_rc], by_rc[b_rc])
            if v is None:
                continue
            dx, dy, resp = v
            if not _keep(float(dx), float(dy), float(resp)):
                continue
            h_shifts.append((float(dx), float(dy)))
            measurements.append((idx_of[a_rc], idx_of[b_rc], float(dx), float(dy), float(resp)))

        for (a_rc, b_rc) in v_edges:
            done_pairs += 1
            if done_pairs % 20 == 0:
                _emit_progress(0.5 + (4.0 * (float(done_pairs) / float(total_pairs))), "Stitching: estimating positions…")
            v = _phase_shift(by_rc[a_rc], by_rc[b_rc])
            if v is None:
                continue
            dx, dy, resp = v
            if not _keep(float(dx), float(dy), float(resp)):
                continue
            v_shifts.append((float(dx), float(dy)))
            measurements.append((idx_of[a_rc], idx_of[b_rc], float(dx), float(dy), float(resp)))

        if len(measurements) < max(1, n - 1):
            vec_col = (
                float(np.median([v[0] for v in h_shifts])) if h_shifts else float(tile_w),
                float(np.median([v[1] for v in h_shifts])) if h_shifts else 0.0,
            )
            vec_row = (
                float(np.median([v[0] for v in v_shifts])) if v_shifts else 0.0,
                float(np.median([v[1] for v in v_shifts])) if v_shifts else float(tile_h),
            )
            pos: dict[tuple[int, int], tuple[float, float]] = {}
            for (r, c) in keys:
                x = (float(c) * float(vec_col[0])) + (float(r) * float(vec_row[0]))
                y = (float(c) * float(vec_col[1])) + (float(r) * float(vec_row[1]))
                pos[(int(r), int(c))] = (float(x), float(y))
            return pos

        m = int(len(measurements))
        A = np.zeros((int(m) + 1, int(n)), dtype=np.float64)
        bx = np.zeros((int(m) + 1,), dtype=np.float64)
        by = np.zeros((int(m) + 1,), dtype=np.float64)
        for row, (ia, ib, dx, dy, wgt) in enumerate(measurements):
            w = math.sqrt(max(1e-6, float(wgt)))
            A[int(row), int(ia)] = -1.0 * float(w)
            A[int(row), int(ib)] = +1.0 * float(w)
            bx[int(row)] = float(dx) * float(w)
            by[int(row)] = float(dy) * float(w)
        A[int(m), 0] = 1.0
        bx[int(m)] = 0.0
        by[int(m)] = 0.0

        x_sol, *_ = np.linalg.lstsq(A, bx, rcond=None)
        y_sol, *_ = np.linalg.lstsq(A, by, rcond=None)
        pos = {}
        for k, i in idx_of.items():
            pos[k] = (float(x_sol[int(i)]), float(y_sol[int(i)]))
        return pos

    _emit_progress(0.0, "Stitching: estimating tile positions…")
    pos = _solve_global_positions()
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    min_x = float(min(xs)) if xs else 0.0
    min_y = float(min(ys)) if ys else 0.0
    for k, (x, y) in list(pos.items()):
        pos[k] = (float(x) - float(min_x), float(y) - float(min_y))

    def _compose(
        images: list["np.ndarray"], masks: list["np.ndarray"], pos: list[tuple[int, int]], *, fuse: str = "notFuse"
    ) -> tuple["np.ndarray", "np.ndarray"]:
        ys = [int(y) for (y, _x) in pos]
        xs = [int(x) for (_y, x) in pos]
        hs = [int(im.shape[0]) for im in images]
        ws = [int(im.shape[1]) for im in images]
        H = max(int(y) + int(h) for y, h in zip(ys, hs))
        W = max(int(x) + int(w) for x, w in zip(xs, ws))
        H = max(1, int(H))
        W = max(1, int(W))

        out_img = np.zeros((int(H), int(W), 3), np.uint8)
        out_mask = np.zeros((int(H), int(W)), np.uint8)
        fuse_norm = (fuse or "notFuse").strip().lower()
        if fuse_norm not in {"notfuse", "average"}:
            fuse_norm = "notfuse"

        for img, msk, (y0, x0) in zip(images, masks, pos):
            h, w = img.shape[:2]
            y0i = int(y0)
            x0i = int(x0)
            roi_img = out_img[y0i : y0i + int(h), x0i : x0i + int(w)]
            roi_m = out_mask[y0i : y0i + int(h), x0i : x0i + int(w)]
            in_m = (msk > 0)
            out_m = (roi_m > 0)
            only = in_m & (~out_m)
            ov = in_m & out_m
            if only.any():
                roi_img[only] = img[only]
                roi_m[only] = 255
            if ov.any():
                if fuse_norm == "average":
                    a = roi_img[ov].astype(np.uint16)
                    b = img[ov].astype(np.uint16)
                    roi_img[ov] = ((a + b) // 2).astype(np.uint8)
                else:
                    roi_img[ov] = img[ov]
                roi_m[ov] = 255
        return out_img, out_mask

    def _read_node(node: _Node) -> tuple["np.ndarray", "np.ndarray"]:
        img = cv2.imread(str(node.image_path), cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError(f"Failed to read image: {node.image_path}")
        if node.mask_path and os.path.exists(node.mask_path):
            m = cv2.imread(str(node.mask_path), cv2.IMREAD_GRAYSCALE)
        else:
            m = None
        if m is None or m.shape[:2] != img.shape[:2]:
            m = (255 * np.ones(img.shape[:2], np.uint8))
        return img, m

    def _write_node(img: "np.ndarray", mask: "np.ndarray", out_img_path: str, out_mask_path: str) -> None:
        imwrite(cv2, out_img_path, img, tiff_params)
        imwrite(cv2, out_mask_path, mask, [])

    def _pyramid_total_nodes(rows: int, cols: int) -> int:
        total = 0
        r = int(rows)
        c = int(cols)
        while int(r) > 1 or int(c) > 1:
            r = (int(r) + 1) // 2
            c = (int(c) + 1) // 2
            total += int(r) * int(c)
        return int(total)

    _emit_progress(5.0, "Stitching: building pyramid…")
    os.makedirs(pyramid_dir, exist_ok=True)

    current: dict[tuple[int, int], _Node] = {}
    for (r, c), p in by_rc.items():
        x, y = pos.get((int(r), int(c)), (0.0, 0.0))
        current[(int(r), int(c))] = _Node(image_path=str(p), mask_path=None, corner=(int(round(float(x))), int(round(float(y)))))
    cur_rows = int(nrows)
    cur_cols = int(ncols)
    level = 0
    cur_level_dir: str | None = None
    total_nodes = max(1, _pyramid_total_nodes(int(nrows), int(ncols)))
    done_nodes = 0

    final_img: "np.ndarray | None" = None
    try:
        while int(cur_rows) > 1 or int(cur_cols) > 1:
            next_rows = (int(cur_rows) + 1) // 2
            next_cols = (int(cur_cols) + 1) // 2
            next_level_dir = os.path.join(pyramid_dir, f"level_{level:02d}")
            os.makedirs(next_level_dir, exist_ok=True)
            next_map: dict[tuple[int, int], _Node] = {}

            for br in range(int(next_rows)):
                for bc in range(int(next_cols)):
                    items: list[_Node] = []
                    for dr in (0, 1):
                        for dc in (0, 1):
                            r = (2 * int(br)) + int(dr)
                            c = (2 * int(bc)) + int(dc)
                            n = current.get((int(r), int(c)))
                            if n is not None:
                                items.append(n)
                    if not items:
                        continue

                    done_nodes += 1
                    pct = 5.0 + (77.0 * (float(done_nodes) / float(total_nodes)))
                    _emit_progress(
                        float(pct),
                        f"Stitching: level {level} node r{br} c{bc} ({len(items)} imgs) [{done_nodes}/{total_nodes}]",
                    )

                    out_img_path = os.path.join(next_level_dir, f"node_r{br:03d}_c{bc:03d}.tif")
                    out_m_path = os.path.join(next_level_dir, f"node_r{br:03d}_c{bc:03d}_mask.png")

                    if len(items) == 1:
                        # Copy through (image + generated full mask).
                        try:
                            shutil.copyfile(items[0].image_path, out_img_path)
                            img0 = cv2.imread(out_img_path, cv2.IMREAD_COLOR)
                            if img0 is None:
                                raise RuntimeError("Copy produced unreadable image.")
                            m0 = 255 * np.ones(img0.shape[:2], np.uint8)
                            imwrite(cv2, out_m_path, m0, [])
                        except Exception as exc:
                            _write_err(f"pyramid copy level {level} r{br} c{bc}", exc)
                            raise
                        next_map[(int(br), int(bc))] = _Node(
                            image_path=str(out_img_path),
                            mask_path=str(out_m_path),
                            corner=items[0].corner,
                        )
                        continue

                    try:
                        min_x = min(int(n.corner[0]) for n in items)
                        min_y = min(int(n.corner[1]) for n in items)
                        imgs_ms = [_read_node(n) for n in items]
                        imgs = [im for (im, _m) in imgs_ms]
                        msks = [m for (_im, m) in imgs_ms]
                        rel_pos = [(int(n.corner[1]) - int(min_y), int(n.corner[0]) - int(min_x)) for n in items]
                        pano, pano_m = _compose(imgs, msks, rel_pos, fuse="notFuse")
                        _write_node(pano, pano_m, out_img_path, out_m_path)
                    except Exception as exc:
                        _write_err(f"pyramid stitch level {level} r{br} c{bc}", exc)
                        if is_no_space_error(exc):
                            raise RuntimeError(
                                "No space left on device while writing stitched outputs. "
                                "Free disk space or choose a different output folder."
                            ) from exc
                        raise

                    next_map[(int(br), int(bc))] = _Node(
                        image_path=str(out_img_path),
                        mask_path=str(out_m_path),
                        corner=(int(min_x), int(min_y)),
                    )

            if not next_map:
                raise RuntimeError("Stitching produced no pyramid nodes.")

            if cur_level_dir:
                _cleanup_dir(cur_level_dir)
            current = next_map
            cur_rows = int(next_rows)
            cur_cols = int(next_cols)
            cur_level_dir = str(next_level_dir)
            level += 1

        if len(current) != 1:
            raise RuntimeError(f"Expected one final panorama, got {len(current)}.")

        final_node = next(iter(current.values()))
        final_img, final_mask = _read_node(final_node)
        H, W = final_img.shape[:2]

        mosaic_path = os.path.join(out_dir, "mosaic_full.tif")
        if bool(build_pyramidal_tiff):
            try:
                imwrite(cv2, mosaic_path, final_img, tiff_params)
            except Exception as exc:
                _write_err("final write", exc)
                if is_no_space_error(exc):
                    raise RuntimeError(
                        "No space left on device while writing mosaic_full.tif. Free disk space or choose a different output folder."
                    ) from exc
                raise

        meta = {
            "method": "imagestitch-pyramid",
            "rows": int(nrows),
            "cols": int(ncols),
            "levels": int(level),
            "tile_size_px": [int(tile_w), int(tile_h)],
            "mosaic_size_px": [int(W), int(H)],
            "fuse": "notFuse",
        }
        try:
            with open(os.path.join(out_dir, "stitch_meta.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, sort_keys=False)
        except Exception:
            pass

        if bool(build_deepzoom):
            _emit_progress(82.0, "Stitching: DeepZoom…")
            os.makedirs(dz_root, exist_ok=True)

            def _dz_tiles_count(w: int, h: int, tile_size: int) -> int:
                if w <= 0 or h <= 0:
                    return 0
                max_dim = max(int(w), int(h))
                max_level = int(math.ceil(math.log(max_dim, 2))) if max_dim > 1 else 0
                total = 0
                cw = int(w)
                ch = int(h)
                for _level in range(int(max_level), -1, -1):
                    total += int((cw + int(tile_size) - 1) // int(tile_size)) * int(
                        (ch + int(tile_size) - 1) // int(tile_size)
                    )
                    cw = max(1, (int(cw) + 1) // 2)
                    ch = max(1, (int(ch) + 1) // 2)
                return int(total)

            total_tiles = max(1, _dz_tiles_count(int(W), int(H), int(dz_tile_px)))
            written_tiles = 0

            def _tile_progress(n: int) -> None:
                nonlocal written_tiles
                written_tiles += int(n)
                frac = float(written_tiles) / float(total_tiles)
                _emit_progress(82.0 + (18.0 * float(frac)), f"Stitching: DeepZoom tiles {written_tiles}/{total_tiles}")

            dz_base = os.path.join(dz_root, "mosaic")
            try:
                _dz_write_from_array(
                    final_img,
                    dz_base=str(dz_base),
                    tile_size=int(dz_tile_px),
                    fmt=str(dz_fmt),
                    jpeg_quality=int(dz_jpeg_q),
                    progress=_tile_progress,
                )
            except Exception as exc:
                _write_err("deepzoom", exc)
                if is_no_space_error(exc):
                    raise RuntimeError(
                        "No space left on device while writing DeepZoom tiles. Free disk space or choose a different output folder."
                    ) from exc
                raise

            manifest: dict[str, object] = {
                "version": 1,
                "size": [int(W), int(H)],
                "chunks": [{"dzi": "mosaic.dzi", "x": 0, "y": 0, "w": int(W), "h": int(H)}],
            }
            with open(os.path.join(dz_root, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2, sort_keys=False)
    finally:
        if cur_level_dir:
            _cleanup_dir(cur_level_dir)
        try:
            if os.path.isdir(pyramid_dir):
                shutil.rmtree(pyramid_dir, ignore_errors=True)
        except Exception:
            pass

    _emit_progress(100.0, "Stitching: done.")
