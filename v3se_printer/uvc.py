from __future__ import annotations

from dataclasses import dataclass
import time


def _try_import_cv2():
    try:
        import cv2  # type: ignore
    except Exception:
        return None
    return cv2


@dataclass
class UvcCameraConfig:
    width: int = 1280
    height: int = 720
    fps: int = 30
    fourcc: str | None = None  # e.g. "MJPG", "YUY2" (best-effort; backend-specific)
    rotation_deg: int = 0  # 0/90/180/270 (applied in software)

    # Crop amounts as percentages of the full frame (applied in software).
    crop_left_pct: float = 0.0
    crop_top_pct: float = 0.0
    crop_right_pct: float = 0.0
    crop_bottom_pct: float = 0.0

    # Best-effort camera controls (availability depends on backend/device).
    lens_autofocus: bool = True
    lens_focus: float | None = None
    auto_exposure: bool = True
    exposure: float | None = None
    auto_white_balance: bool = True
    white_balance: float | None = None

    brightness: float | None = None
    contrast: float | None = None
    saturation: float | None = None
    hue: float | None = None
    gamma: float | None = None
    gain: float | None = None
    sharpness: float | None = None

    # Printer+camera autofocus (moves printer Z and measures sharpness).
    af_fast_step_mm: float = 1.0
    af_slow_step_mm: float = 0.1
    af_max_travel_mm: float = 10.0
    af_settle_ms: int = 150


@dataclass(frozen=True)
class CameraProbe:
    index: int
    opened: bool
    frame_ok: bool
    info: str


def list_uvc_indices(*, max_index: int = 6) -> list[int]:
    probes = probe_uvc_indices(max_index=max_index)
    return [p.index for p in probes if p.opened and p.frame_ok]


def probe_uvc_indices(*, max_index: int = 6, read_tries: int = 3) -> list[CameraProbe]:
    cv2 = _try_import_cv2()
    if cv2 is None:
        return []

    probes: list[CameraProbe] = []
    for i in range(max(0, int(max_index))):
        def _probe(open_fn) -> tuple[bool, bool, str]:
            cap = None
            try:
                cap = open_fn()
                if not cap.isOpened():
                    return (False, False, "?")
                tries = max(1, int(read_tries))
                frame_ok = False
                last_frame = None
                for t in range(tries):
                    ok, frame = cap.read()
                    if ok and frame is not None:
                        frame_ok = True
                        last_frame = frame
                        break
                    if t < (tries - 1):
                        time.sleep(0.05)
                cap_info = get_capture_info(cap)
                if frame_ok and last_frame is not None:
                    try:
                        h, w = last_frame.shape[:2]
                        cap_info = f"{w}x{h} (frame) | {cap_info}"
                    except Exception:
                        pass
                return (True, frame_ok, cap_info)
            except Exception:
                return (False, False, "?")
            finally:
                try:
                    if cap is not None:
                        cap.release()
                except Exception:
                    pass

        opened_any, frame_any, info_any = _probe(lambda: cv2.VideoCapture(i))

        opened = opened_any
        frame_ok = frame_any
        info = info_any

        if (not frame_ok) and hasattr(cv2, "CAP_AVFOUNDATION"):
            opened_avf, frame_avf, info_avf = _probe(lambda: cv2.VideoCapture(i, cv2.CAP_AVFOUNDATION))
            if frame_avf or (not opened):
                opened = opened_avf
                frame_ok = frame_avf
                info = info_avf

        if opened:
            probes.append(CameraProbe(index=i, opened=True, frame_ok=frame_ok, info=info))
    return probes


def apply_uvc_config(cap, cfg: UvcCameraConfig) -> None:
    cv2 = _try_import_cv2()
    if cv2 is None:
        return

    def _set(prop: int, val: float) -> bool:
        try:
            return bool(cap.set(prop, float(val)))
        except Exception:
            return False

    if cfg.fourcc:
        fmt = str(cfg.fourcc).strip().upper()
        if len(fmt) == 4:
            try:
                code = cv2.VideoWriter_fourcc(*fmt)
                _set(cv2.CAP_PROP_FOURCC, float(code))
            except Exception:
                pass

    if cfg.width > 0:
        _set(cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
    if cfg.height > 0:
        _set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)
    if cfg.fps > 0:
        _set(cv2.CAP_PROP_FPS, cfg.fps)

    # Lens controls (best effort; backend-specific)
    try:
        _set(cv2.CAP_PROP_AUTOFOCUS, 1.0 if cfg.lens_autofocus else 0.0)
    except Exception:
        pass
    if (cfg.lens_focus is not None) and (not cfg.lens_autofocus):
        try:
            _set(cv2.CAP_PROP_FOCUS, cfg.lens_focus)
        except Exception:
            pass

    # Exposure controls are notoriously backend-specific. We attempt reasonable defaults:
    # - Some backends expect 0.75 (auto) / 0.25 (manual) for CAP_PROP_AUTO_EXPOSURE.
    # - Some ignore it entirely.
    try:
        if cfg.auto_exposure:
            _set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75)
            _set(cv2.CAP_PROP_AUTO_EXPOSURE, 1.0)
        else:
            _set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)
            _set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.0)
    except Exception:
        pass
    if (cfg.exposure is not None) and (not cfg.auto_exposure):
        try:
            _set(cv2.CAP_PROP_EXPOSURE, cfg.exposure)
        except Exception:
            pass

    try:
        _set(cv2.CAP_PROP_AUTO_WB, 1.0 if cfg.auto_white_balance else 0.0)
    except Exception:
        pass
    if (cfg.white_balance is not None) and (not cfg.auto_white_balance):
        try:
            _set(cv2.CAP_PROP_WB_TEMPERATURE, cfg.white_balance)
        except Exception:
            pass

    if cfg.brightness is not None:
        _set(cv2.CAP_PROP_BRIGHTNESS, cfg.brightness)
    if cfg.contrast is not None:
        _set(cv2.CAP_PROP_CONTRAST, cfg.contrast)
    if cfg.saturation is not None:
        _set(cv2.CAP_PROP_SATURATION, cfg.saturation)
    if cfg.hue is not None:
        _set(cv2.CAP_PROP_HUE, cfg.hue)
    if cfg.gamma is not None:
        _set(cv2.CAP_PROP_GAMMA, cfg.gamma)
    if cfg.gain is not None:
        _set(cv2.CAP_PROP_GAIN, cfg.gain)
    if cfg.sharpness is not None and hasattr(cv2, "CAP_PROP_SHARPNESS"):
        _set(cv2.CAP_PROP_SHARPNESS, cfg.sharpness)


def get_capture_info(cap) -> str:
    cv2 = _try_import_cv2()
    if cv2 is None:
        return "?"

    def _get(prop: int) -> float | None:
        try:
            v = float(cap.get(prop))
        except Exception:
            return None
        if v != v:
            return None
        return v

    w = _get(cv2.CAP_PROP_FRAME_WIDTH)
    h = _get(cv2.CAP_PROP_FRAME_HEIGHT)
    fps = _get(cv2.CAP_PROP_FPS)
    fourcc = _get(cv2.CAP_PROP_FOURCC)
    w_s = "?" if w is None else str(int(round(w)))
    h_s = "?" if h is None else str(int(round(h)))
    fps_s = "?" if fps is None else f"{fps:.1f}"
    fmt_s = "?" if fourcc is None else decode_fourcc(fourcc)
    return f"{w_s}x{h_s} @ {fps_s} fps | {fmt_s}"


def decode_fourcc(val: float | int) -> str:
    try:
        code = int(val)
    except Exception:
        return "?"
    if code == 0:
        return "?"
    out = []
    for i in range(4):
        ch = (code >> (8 * i)) & 0xFF
        out.append(chr(ch) if 32 <= ch <= 126 else "?")
    s = "".join(out)
    if s.strip("?\x00").strip() == "":
        return "?"
    return s


def transform_frame(
    frame,
    *,
    rotation_deg: int = 0,
    crop_left_pct: float = 0.0,
    crop_top_pct: float = 0.0,
    crop_right_pct: float = 0.0,
    crop_bottom_pct: float = 0.0,
    max_width: int | None = None,
):
    cv2 = _try_import_cv2()
    if cv2 is None:
        raise RuntimeError("OpenCV (cv2) not installed")

    img = frame
    try:
        rot = int(rotation_deg) % 360
    except Exception:
        rot = 0

    try:
        if rot == 90:
            img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
        elif rot == 180:
            img = cv2.rotate(img, cv2.ROTATE_180)
        elif rot == 270:
            img = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    except Exception:
        img = frame

    h, w = img.shape[:2]
    l = max(0.0, float(crop_left_pct)) / 100.0
    t = max(0.0, float(crop_top_pct)) / 100.0
    r = max(0.0, float(crop_right_pct)) / 100.0
    b = max(0.0, float(crop_bottom_pct)) / 100.0
    l = min(0.49, l)
    r = min(0.49, r)
    t = min(0.49, t)
    b = min(0.49, b)

    x0 = int(round(w * l))
    x1 = int(round(w * (1.0 - r)))
    y0 = int(round(h * t))
    y1 = int(round(h * (1.0 - b)))
    if (x1 - x0) >= 16 and (y1 - y0) >= 16:
        img = img[y0:y1, x0:x1]

    if max_width is not None:
        try:
            mw = int(max_width)
        except Exception:
            mw = 0
        if mw > 0 and img.shape[1] > mw:
            try:
                scale = mw / float(img.shape[1])
                img = cv2.resize(img, dsize=None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            except Exception:
                pass

    return img


def compute_sharpness(
    frame,
    *,
    rotation_deg: int = 0,
    crop_left_pct: float = 0.0,
    crop_top_pct: float = 0.0,
    crop_right_pct: float = 0.0,
    crop_bottom_pct: float = 0.0,
) -> float:
    cv2 = _try_import_cv2()
    if cv2 is None:
        raise RuntimeError("OpenCV (cv2) not installed")

    img = transform_frame(
        frame,
        rotation_deg=rotation_deg,
        crop_left_pct=crop_left_pct,
        crop_top_pct=crop_top_pct,
        crop_right_pct=crop_right_pct,
        crop_bottom_pct=crop_bottom_pct,
    )

    # Downscale a bit to reduce CPU and noise sensitivity.
    try:
        max_w = 640
        if img.shape[1] > max_w:
            scale = max_w / float(img.shape[1])
            img = cv2.resize(img, dsize=None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    except Exception:
        pass

    if len(img.shape) == 2:
        gray = img
    elif img.shape[2] == 4:
        gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
    else:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    # Variance of Laplacian is a common sharpness proxy.
    return float(lap.var())
