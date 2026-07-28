from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import cv2
import numpy as np

from camera_server.server import (
    CameraBusyError,
    CameraHTTPServer,
    CameraManager,
    CameraSelectionError,
    CameraStream,
    DEFAULT_PROFILE,
    FHD_HEIGHT,
    FHD_WIDTH,
    PREVIEW_PROFILES,
    STALE_AFTER_SECONDS,
    UHD_HEIGHT,
    UHD_WIDTH,
    WhiteBalanceError,
    normalize_white_balance_roi,
    parse_args,
)
from v3se_printer.uvc import CameraProbe


class FakeFrame:
    def __init__(self, width: int, height: int) -> None:
        self.shape = (height, width, 3)


class FakeEncoded:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def tobytes(self) -> bytes:
        return self.payload


class FakeCapture:
    def __init__(
        self,
        width: int = UHD_WIDTH,
        height: int = UHD_HEIGHT,
        frames: list[object] | None = None,
    ) -> None:
        self.frames = frames or [FakeFrame(width, height)]
        self.read_index = 0
        self.opened = True
        self.release_count = 0
        self.set_calls: list[tuple[int, float]] = []

    def isOpened(self) -> bool:
        return self.opened

    def read(self) -> tuple[bool, object | None]:
        time.sleep(0.002)
        if not self.opened:
            return False, None
        frame = self.frames[min(self.read_index, len(self.frames) - 1)]
        self.read_index += 1
        return True, frame

    def set(self, prop: int, value: float) -> bool:
        self.set_calls.append((prop, value))
        return True

    def release(self) -> None:
        self.opened = False
        self.release_count += 1


class FakeCV2:
    CAP_PROP_BUFFERSIZE = 38
    IMWRITE_JPEG_QUALITY = 1

    def __init__(self, encode_delay: float = 0.0) -> None:
        self.encode_count = 0
        self.encode_delay = encode_delay

    def imencode(self, extension: str, frame: object, params: list[int]) -> tuple[bool, FakeEncoded]:
        time.sleep(self.encode_delay)
        self.encode_count += 1
        payload = b"\xff\xd8FAKE-4K-JPEG\xff\xd9"
        return True, FakeEncoded(payload)

    def LUT(self, frame: object, _lut: object) -> object:
        return frame


def make_stream(
    capture: FakeCapture,
    cv2_module: object | None = None,
    camera_index: int = 2,
    *,
    width: int = UHD_WIDTH,
    height: int = UHD_HEIGHT,
    stream_fps: int = 30,
    jpeg_quality: int = 88,
    red_gain: float = 1.0,
    green_gain: float = 1.0,
    blue_gain: float = 1.0,
) -> tuple[CameraStream, object, list[object]]:
    cv2_module = cv2_module or FakeCV2()
    configured: list[object] = []
    stream = CameraStream(
        camera_index=camera_index,
        fps=30,
        fourcc="MJPG",
        jpeg_quality=jpeg_quality,
        width=width,
        height=height,
        stream_fps=stream_fps,
        red_gain=red_gain,
        green_gain=green_gain,
        blue_gain=blue_gain,
        cv2_module=cv2_module,
        capture_factory=lambda _index, _backend: capture,
        configurer=lambda _capture, config: configured.append(config),
        capture_info=lambda _capture: "3840x2160 @ 30 fps | MJPG",
        negotiation_timeout=0.1,
    )
    return stream, cv2_module, configured


class BlockingCapture(FakeCapture):
    def __init__(self) -> None:
        super().__init__()
        self.blocked = threading.Event()
        self.unblock = threading.Event()

    def read(self) -> tuple[bool, object | None]:
        if self.read_index == 0:
            return super().read()
        self.blocked.set()
        self.unblock.wait(5.0)
        if not self.opened:
            return False, None
        return super().read()

    def release(self) -> None:
        super().release()
        self.unblock.set()


class FailingCapture(FakeCapture):
    def __init__(self, successful_reads: int = 20) -> None:
        super().__init__()
        self.successful_reads = successful_reads

    def read(self) -> tuple[bool, object | None]:
        if self.read_index < self.successful_reads:
            return super().read()
        time.sleep(0.002)
        return False, None


class CameraStreamTests(unittest.TestCase):
    def test_starts_only_after_a_jpeg_is_ready(self) -> None:
        capture = FakeCapture()
        stream, cv2, configured = make_stream(capture)
        stream.start(timeout=1.0)
        stream.stop()

        status = stream.status()
        self.assertEqual((status["actual"]["width"], status["actual"]["height"]), (UHD_WIDTH, UHD_HEIGHT))
        self.assertGreaterEqual(cv2.encode_count, 1)
        self.assertEqual(configured[0].fourcc, "MJPG")
        self.assertFalse(configured[0].auto_white_balance)
        self.assertIn((FakeCV2.CAP_PROP_BUFFERSIZE, 1.0), capture.set_calls)
        self.assertEqual(capture.release_count, 1)

    def test_accepts_a_camera_that_falls_back_to_1080p(self) -> None:
        capture = FakeCapture(1920, 1080)
        stream, _cv2, _configured = make_stream(capture)
        stream.start(timeout=1.0)
        self.addCleanup(stream.stop)

        status = stream.status()
        self.assertTrue(status["healthy"])
        self.assertEqual((status["actual"]["width"], status["actual"]["height"]), (1920, 1080))

    def test_accepts_4k_after_a_transient_warmup_frame(self) -> None:
        capture = FakeCapture(frames=[FakeFrame(1920, 1080), FakeFrame(UHD_WIDTH, UHD_HEIGHT)])
        stream, _cv2, _configured = make_stream(capture)
        stream.start(timeout=1.0)
        self.addCleanup(stream.stop)

        self.assertTrue(stream.status()["healthy"])

    def test_stop_releases_a_capture_blocked_in_read(self) -> None:
        capture = BlockingCapture()
        stream, _cv2, _configured = make_stream(capture)
        stream.start(timeout=1.0)
        self.assertTrue(capture.blocked.wait(1.0))

        self.assertTrue(stream.stop())
        self.assertEqual(capture.release_count, 1)

    def test_stale_jpeg_is_reported_as_stalled(self) -> None:
        capture = BlockingCapture()
        stream, _cv2, _configured = make_stream(capture)
        stream.start(timeout=1.0)
        self.addCleanup(stream.stop)
        self.assertTrue(capture.blocked.wait(1.0))
        with stream._condition:
            stream._jpeg_at = time.monotonic() - STALE_AFTER_SECONDS - 1.0

        status = stream.status()
        self.assertEqual(status["state"], "stalled")
        self.assertFalse(status["healthy"])
        self.assertEqual(status["actual"]["fps"], 0.0)

    def test_reports_encoded_fps_separately_from_capture_fps(self) -> None:
        capture = FakeCapture()
        stream, _cv2, _configured = make_stream(capture, FakeCV2(encode_delay=0.02))
        stream.start(timeout=1.0)
        self.addCleanup(stream.stop)
        time.sleep(0.12)

        actual = stream.status()["actual"]
        self.assertGreater(actual["capture_fps"], actual["fps"])

    def test_preview_rate_is_paced_without_blocking_capture(self) -> None:
        stream, _cv2, _configured = make_stream(FakeCapture(), stream_fps=10)
        stream.start(timeout=1.0)
        self.addCleanup(stream.stop)
        first_sequence = stream.status()["jpeg"]["sequence"]
        time.sleep(0.36)

        status = stream.status()
        encoded = status["jpeg"]["sequence"] - first_sequence
        self.assertGreaterEqual(encoded, 3)
        self.assertLessEqual(encoded, 4)
        self.assertLessEqual(status["actual"]["fps"], 11.0)
        self.assertGreater(status["actual"]["capture_fps"], status["actual"]["fps"])

    def test_runtime_color_gains_adjust_encoded_channels(self) -> None:
        frame = np.full((32, 32, 3), 100, dtype=np.uint8)
        capture = FakeCapture(width=32, height=32, frames=[frame])
        stream, _cv2, _configured = make_stream(
            capture,
            cv2,
            width=32,
            height=32,
            jpeg_quality=100,
        )
        stream.start(timeout=2.0)
        self.addCleanup(stream.stop)
        sequence = stream.status()["jpeg"]["sequence"]
        stream.set_color_gains(1.2, 0.8, 1.0)
        for _ in range(2):
            item = stream.wait_for_jpeg(sequence, timeout=1.0)
            self.assertIsNotNone(item)
            sequence, jpeg = item

        encoded = np.frombuffer(jpeg, dtype=np.uint8)
        decoded = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        blue, green, red = decoded.mean(axis=(0, 1))
        self.assertAlmostEqual(blue, 100, delta=3)
        self.assertAlmostEqual(green, 80, delta=3)
        self.assertAlmostEqual(red, 120, delta=3)

    def test_gray_region_white_balance_ignores_the_rest_of_the_frame(self) -> None:
        frame = np.full((100, 100, 3), [180, 80, 60], dtype=np.uint8)
        frame[25:75, 25:75] = [80, 160, 120]

        gains, sample = CameraStream._estimate_white_balance(frame, (0.25, 0.25, 0.5, 0.5))

        self.assertEqual(sample, (120.0, 160.0, 80.0))
        self.assertAlmostEqual(gains[0], 1.0)
        self.assertAlmostEqual(gains[1], 0.75)
        self.assertAlmostEqual(gains[2], 1.5)

    def test_picked_gray_region_neutralizes_the_encoded_frame(self) -> None:
        frame = np.full((64, 64, 3), [80, 160, 120], dtype=np.uint8)
        stream, _cv2, _configured = make_stream(
            FakeCapture(width=64, height=64, frames=[frame]),
            cv2,
            width=64,
            height=64,
            jpeg_quality=100,
        )
        stream.start(timeout=2.0)
        self.addCleanup(stream.stop)
        stream.configure_white_balance(False, (0.0, 0.0, 1.0, 1.0))
        sequence = stream.status()["jpeg"]["sequence"]
        item = stream.wait_for_jpeg(sequence, timeout=1.0)
        self.assertIsNotNone(item)

        encoded = np.frombuffer(item[1], dtype=np.uint8)
        decoded = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        blue, green, red = decoded.mean(axis=(0, 1))
        self.assertAlmostEqual(red, green, delta=3)
        self.assertAlmostEqual(green, blue, delta=3)
        self.assertFalse(stream.status()["color"]["auto_white_balance"])

    def test_auto_white_balance_is_paced_smoothed_and_rejects_dark_pixels(self) -> None:
        stream, _cv2, _configured = make_stream(FakeCapture())
        roi = (0.0, 0.0, 1.0, 1.0)
        first = np.full((32, 32, 3), [80, 160, 120], dtype=np.uint8)
        second = np.full((32, 32, 3), [120, 80, 100], dtype=np.uint8)
        stream.configure_white_balance(True, roi, calibrate=False)

        stream._update_auto_white_balance(first, 1.0)
        self.assertEqual(stream.color_gains(), (1.0, 0.75, 1.5))
        stream._update_auto_white_balance(second, 1.2)
        self.assertEqual(stream.color_gains(), (1.0, 0.75, 1.5))
        stream._update_auto_white_balance(second, 1.6)
        red, green, blue = stream.color_gains()
        self.assertAlmostEqual(red, 1.0)
        self.assertAlmostEqual(green, 0.875)
        self.assertAlmostEqual(blue, 4.0 / 3.0)

        gains = stream.color_gains()
        stream._update_auto_white_balance(np.zeros((32, 32, 3), dtype=np.uint8), 2.2)
        color = stream.status()["color"]
        self.assertEqual(stream.color_gains(), gains)
        self.assertEqual(color["auto_white_balance_state"], "waiting")
        self.assertIn("too dark", color["white_balance_error"])

        mostly_invalid = np.zeros((100, 100, 3), dtype=np.uint8)
        mostly_invalid[45:55, 45:55] = [80, 160, 120]
        with self.assertRaisesRegex(WhiteBalanceError, "too dark"):
            CameraStream._estimate_white_balance(mostly_invalid, roi)

    def test_manual_gains_disable_auto_white_balance(self) -> None:
        stream, _cv2, _configured = make_stream(FakeCapture())
        stream.configure_white_balance(True, (0.0, 0.0, 1.0, 1.0), calibrate=False)
        stream._update_auto_white_balance(np.zeros((32, 32, 3), dtype=np.uint8), 1.0)

        stream.set_color_gains(1.1, 0.9, 1.0)

        color = stream.status()["color"]
        self.assertFalse(color["auto_white_balance"])
        self.assertEqual(color["auto_white_balance_state"], "disabled")
        self.assertIsNone(color["white_balance_error"])
        self.assertIsNone(color["white_balance_sample"])

    def test_in_flight_auto_estimate_cannot_overwrite_manual_gains(self) -> None:
        stream, _cv2, _configured = make_stream(FakeCapture())
        stream.configure_white_balance(True, (0.0, 0.0, 1.0, 1.0), calibrate=False)
        estimate_started = threading.Event()
        finish_estimate = threading.Event()
        original_estimator = stream._estimate_white_balance

        def blocked_estimator(frame: object, roi: tuple[float, float, float, float]):
            estimate_started.set()
            finish_estimate.wait(1.0)
            return original_estimator(frame, roi)

        stream._estimate_white_balance = blocked_estimator
        frame = np.full((32, 32, 3), [80, 160, 120], dtype=np.uint8)
        thread = threading.Thread(target=stream._update_auto_white_balance, args=(frame, 1.0))
        thread.start()
        self.assertTrue(estimate_started.wait(1.0))
        stream.set_color_gains(1.1, 0.9, 1.0)
        finish_estimate.set()
        thread.join(timeout=1.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(stream.color_gains(), (1.1, 0.9, 1.0))
        self.assertFalse(stream.status()["color"]["auto_white_balance"])

    def test_failed_gray_sample_preserves_the_previous_settings(self) -> None:
        stream, _cv2, _configured = make_stream(FakeCapture())
        with stream._condition:
            stream._raw_frame = np.full((32, 32, 3), [80, 160, 120], dtype=np.uint8)
        stream.configure_white_balance(False, (0.0, 0.0, 1.0, 1.0))
        original = stream.color_settings()
        with stream._condition:
            stream._raw_frame = np.zeros((32, 32, 3), dtype=np.uint8)

        with self.assertRaisesRegex(WhiteBalanceError, "too dark"):
            stream.configure_white_balance(True, (0.0, 0.0, 1.0, 1.0))

        self.assertEqual(stream.color_settings(), original)

    def test_white_balance_roi_validation(self) -> None:
        self.assertEqual(
            normalize_white_balance_roi({"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}),
            (0.1, 0.2, 0.3, 0.4),
        )
        invalid = [
            {"x": 0, "y": 0, "width": 0.005, "height": 0.5},
            {"x": 0.8, "y": 0, "width": 0.3, "height": 0.5},
            {"x": False, "y": 0, "width": 0.5, "height": 0.5},
        ]
        for roi in invalid:
            with self.subTest(roi=roi), self.assertRaises(ValueError):
                normalize_white_balance_roi(roi)

    def test_capture_failure_after_startup_is_reported(self) -> None:
        capture = FailingCapture()
        stream, _cv2, _configured = make_stream(capture)
        stream.start(timeout=1.0)
        self.addCleanup(stream.stop)
        deadline = time.monotonic() + 2.0
        while stream.status()["state"] != "error" and time.monotonic() < deadline:
            time.sleep(0.02)

        self.assertEqual(stream.status()["state"], "error")
        self.assertFalse(stream.status()["healthy"])
        with self.assertRaisesRegex(RuntimeError, "10 consecutive empty frames"):
            stream.raise_if_failed()

    def test_slow_consumers_receive_the_latest_encoded_frame(self) -> None:
        capture = FakeCapture()
        stream, _cv2, _configured = make_stream(capture)
        stream.start(timeout=1.0)
        self.addCleanup(stream.stop)

        first = stream.wait_for_jpeg(0, timeout=1.0)
        self.assertIsNotNone(first)
        first_sequence = first[0]
        time.sleep(0.04)
        latest = stream.wait_for_jpeg(first_sequence, timeout=1.0)

        self.assertIsNotNone(latest)
        self.assertGreater(latest[0], first_sequence)

    def test_manager_scans_selects_switches_and_stops(self) -> None:
        captures: dict[int, FakeCapture] = {}

        profiles: list[str] = []

        def stream_factory(camera_index: int, profile: str) -> CameraStream:
            profiles.append(profile)
            capture = FakeCapture()
            captures[camera_index] = capture
            return make_stream(capture, camera_index=camera_index)[0]

        manager = CameraManager(
            fps=30,
            fourcc="MJPG",
            jpeg_quality=88,
            scan_limit=4,
            stream_factory=stream_factory,
            probe=lambda _limit: [
                CameraProbe(1, True, True, "3840x2160"),
                CameraProbe(3, True, False, "opened without a frame"),
            ],
        )
        self.addCleanup(manager.shutdown)

        idle = manager.status()
        self.assertEqual(idle["profile"], DEFAULT_PROFILE)
        self.assertEqual((idle["requested"]["width"], idle["requested"]["height"]), (UHD_WIDTH, UHD_HEIGHT))

        scan = manager.scan()
        self.assertEqual([item["index"] for item in scan["cameras"]], [1, 3])
        first = manager.select(1)
        self.assertEqual(first["camera_index"], 1)
        self.assertEqual(first["generation"], 1)
        second = manager.select(3, "detail")
        self.assertEqual(second["camera_index"], 3)
        self.assertEqual(second["profile"], "detail")
        self.assertEqual(second["generation"], 2)
        self.assertEqual(profiles, [DEFAULT_PROFILE, "detail"])
        self.assertEqual(captures[1].release_count, 1)

        stopped = manager.stop()
        self.assertEqual(stopped["state"], "idle")
        self.assertIsNone(manager.current_stream())
        self.assertEqual(captures[3].release_count, 1)

    def test_manager_recovers_after_a_camera_selection_failure(self) -> None:
        def stream_factory(index: int, _profile: str) -> CameraStream:
            capture = FakeCapture()
            capture.opened = False
            return make_stream(capture, camera_index=index)[0]

        manager = CameraManager(
            fps=30,
            fourcc="MJPG",
            jpeg_quality=88,
            scan_limit=2,
            stream_factory=stream_factory,
            probe=lambda _limit: [],
        )
        self.addCleanup(manager.shutdown)

        with self.assertRaisesRegex(CameraSelectionError, "could not open device"):
            manager.select(1)

        status = manager.status()
        self.assertEqual(status["state"], "error")
        self.assertFalse(status["healthy"])
        self.assertIsNone(manager.current_stream())
        self.assertEqual(manager.scan()["state"], "ready")
        self.assertEqual(manager.status()["state"], "idle")

    def test_manager_serializes_scans_and_selections(self) -> None:
        scan_started = threading.Event()
        finish_scan = threading.Event()

        def probe(_limit: int) -> list[CameraProbe]:
            scan_started.set()
            finish_scan.wait(2.0)
            return []

        manager = CameraManager(30, "MJPG", 88, 2, probe=probe)
        self.addCleanup(manager.shutdown)
        thread = threading.Thread(target=manager.scan)
        thread.start()
        self.assertTrue(scan_started.wait(1.0))

        with self.assertRaises(CameraBusyError):
            manager.select(0)

        finish_scan.set()
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(manager.cameras()["state"], "ready")

    def test_color_update_during_selection_reaches_candidate(self) -> None:
        factory_started = threading.Event()
        finish_factory = threading.Event()
        streams: list[CameraStream] = []

        def stream_factory(_camera_index: int, _profile: str) -> CameraStream:
            stream = make_stream(FakeCapture())[0]
            streams.append(stream)
            factory_started.set()
            finish_factory.wait(2.0)
            return stream

        manager = CameraManager(30, "MJPG", 80, 2, stream_factory=stream_factory, probe=lambda _limit: [])
        self.addCleanup(manager.shutdown)
        executor = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(executor.shutdown)
        future = executor.submit(manager.select, 0)
        self.assertTrue(factory_started.wait(1.0))

        manager.set_color_gains(1.1, 0.8, 1.05)
        finish_factory.set()
        selected = future.result(timeout=2.0)

        self.assertEqual(selected["state"], "streaming")
        self.assertIs(manager.current_stream(), streams[0])
        self.assertEqual(streams[0].color_gains(), (1.1, 0.8, 1.05))

    def test_manager_preserves_gray_region_for_profile_changes_but_not_camera_changes(self) -> None:
        frames = {
            0: np.full((64, 64, 3), [80, 160, 120], dtype=np.uint8),
            1: np.full((64, 64, 3), [100, 100, 150], dtype=np.uint8),
        }

        def stream_factory(camera_index: int, _profile: str) -> CameraStream:
            return make_stream(
                FakeCapture(width=64, height=64, frames=[frames[camera_index]]),
                camera_index=camera_index,
                width=64,
                height=64,
            )[0]

        manager = CameraManager(30, "MJPG", 80, 2, stream_factory=stream_factory, probe=lambda _limit: [])
        self.addCleanup(manager.shutdown)
        first = manager.select(0)
        roi = {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}
        balanced = manager.configure_white_balance(True, roi, first["generation"])
        self.assertEqual(balanced["color"]["auto_white_balance_state"], "active")
        self.assertEqual(balanced["color"]["white_balance_roi"], roi)

        second = manager.select(0, "detail")
        self.assertTrue(second["color"]["auto_white_balance"])
        self.assertEqual(second["color"]["white_balance_roi"], roi)

        third = manager.select(1)
        self.assertFalse(third["color"]["auto_white_balance"])
        self.assertIsNone(third["color"]["white_balance_roi"])
        self.assertEqual(
            (third["color"]["red_gain"], third["color"]["green_gain"], third["color"]["blue_gain"]),
            (1.0, 1.0, 1.0),
        )
        with self.assertRaisesRegex(WhiteBalanceError, "preview changed"):
            manager.configure_white_balance(False, generation=first["generation"])

        manual = manager.set_color_gains(1.1, 0.9, 1.0)
        self.assertFalse(manual["color"]["auto_white_balance"])

    def test_http_picker_status_snapshot_health_and_mjpeg(self) -> None:
        streams: list[CameraStream] = []

        def stream_factory(camera_index: int, _profile: str) -> CameraStream:
            frame = np.full((64, 64, 3), [80, 160, 120], dtype=np.uint8)
            stream = make_stream(
                FakeCapture(width=64, height=64, frames=[frame]),
                camera_index=camera_index,
                width=64,
                height=64,
            )[0]
            streams.append(stream)
            return stream

        manager = CameraManager(
            fps=30,
            fourcc="MJPG",
            jpeg_quality=88,
            scan_limit=4,
            stream_factory=stream_factory,
            probe=lambda _limit: [CameraProbe(2, True, True, "3840x2160")],
        )
        server = CameraHTTPServer(("127.0.0.1", 0), manager)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            root = f"http://127.0.0.1:{server.server_address[1]}"
            with urlopen(root + "/", timeout=2.0) as response:
                page = response.read()
                self.assertIn(b'id="camera"', page)
                self.assertIn(b'fetch("/cameras/scan"', page)
                self.assertIn(b'fetch("/cameras.json"', page)
                self.assertIn(b'id="auto-white-balance"', page)
                self.assertIn(b'id="pick-gray"', page)
                self.assertIn(b'id="white-balance-overlay"', page)
                self.assertIn(b'fetch("/white-balance"', page)
                self.assertIn(b"initialize();", page)
            with urlopen(root + "/status.json", timeout=2.0) as response:
                status = json.load(response)
                self.assertEqual(status["state"], "idle")
                self.assertFalse(status["healthy"])
            with self.assertRaises(HTTPError) as unavailable:
                urlopen(root + "/snapshot.jpg", timeout=2.0)
            self.assertEqual(unavailable.exception.code, 503)
            with self.assertRaises(HTTPError) as forbidden:
                urlopen(Request(root + "/cameras/scan", method="POST"), timeout=2.0)
            self.assertEqual(forbidden.exception.code, 403)

            scan_request = Request(
                root + "/cameras/scan",
                method="POST",
                headers={"X-MarlinScan-Request": "1"},
            )
            with urlopen(scan_request, timeout=2.0) as response:
                scan = json.load(response)
            self.assertEqual(scan["cameras"][0]["index"], 2)

            body = json.dumps({"camera_index": 2, "profile": "detail"}).encode("utf-8")
            select_request = Request(
                root + "/camera",
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-MarlinScan-Request": "1",
                },
            )
            with urlopen(select_request, timeout=2.0) as response:
                status = json.load(response)
                self.assertTrue(status["healthy"])
                self.assertEqual(status["profile"], "detail")
                self.assertEqual(status["actual"]["width"], 64)
                generation = status["generation"]

            settings_body = json.dumps(
                {"red_gain": 1.1, "green_gain": 0.85, "blue_gain": 1.05}
            ).encode("utf-8")
            settings_request = Request(
                root + "/settings",
                data=settings_body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-MarlinScan-Request": "1",
                },
            )
            with urlopen(settings_request, timeout=2.0) as response:
                color = json.load(response)["color"]
            self.assertEqual(
                {name: color[name] for name in ("red_gain", "green_gain", "blue_gain")},
                {"red_gain": 1.1, "green_gain": 0.85, "blue_gain": 1.05},
            )
            self.assertFalse(color["auto_white_balance"])

            white_balance_body = json.dumps(
                {
                    "auto_white_balance": True,
                    "white_balance_roi": {"x": 0, "y": 0, "width": 1, "height": 1},
                    "generation": generation,
                }
            ).encode("utf-8")
            white_balance_request = Request(
                root + "/white-balance",
                data=white_balance_body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-MarlinScan-Request": "1",
                },
            )
            with urlopen(white_balance_request, timeout=2.0) as response:
                color = json.load(response)["color"]
            self.assertTrue(color["auto_white_balance"])
            self.assertEqual(color["auto_white_balance_state"], "active")
            self.assertAlmostEqual(color["red_gain"], 1.0)
            self.assertAlmostEqual(color["green_gain"], 0.75)
            self.assertAlmostEqual(color["blue_gain"], 1.5)

            stale_body = json.dumps(
                {"auto_white_balance": False, "generation": generation - 1}
            ).encode("utf-8")
            stale_request = Request(
                root + "/white-balance",
                data=stale_body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-MarlinScan-Request": "1",
                },
            )
            with self.assertRaises(HTTPError) as stale:
                urlopen(stale_request, timeout=2.0)
            self.assertEqual(stale.exception.code, 422)
            time.sleep(0.05)
            with urlopen(root + "/status.json", timeout=2.0) as response:
                self.assertTrue(json.load(response)["healthy"])
            with urlopen(root + "/snapshot.jpg", timeout=2.0) as response:
                self.assertEqual(response.headers.get_content_type(), "image/jpeg")
                self.assertEqual(response.read(), streams[0].latest_jpeg())
            with urlopen(root + "/healthz", timeout=2.0) as response:
                self.assertEqual(response.status, 200)
            response = urlopen(root + "/stream.mjpg", timeout=2.0)
            try:
                self.assertEqual(response.headers.get_content_type(), "multipart/x-mixed-replace")
                self.assertEqual(response.readline(), b"--frame\r\n")
                self.assertEqual(response.readline(), b"Content-Type: image/jpeg\r\n")
                length = int(response.readline().decode("ascii").split(":", 1)[1])
                self.assertEqual(response.readline(), b"\r\n")
                self.assertEqual(response.read(length), streams[0].latest_jpeg())
                self.assertEqual(response.read(2), b"\r\n")
            finally:
                response.close()

            stop_request = Request(
                root + "/camera",
                method="DELETE",
                headers={"X-MarlinScan-Request": "1"},
            )
            with urlopen(stop_request, timeout=2.0) as response:
                self.assertEqual(json.load(response)["state"], "idle")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)
            manager.shutdown()

    def test_cli_defaults_to_detail_preview_settings(self) -> None:
        args = parse_args([])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.fps, 30)
        self.assertEqual(args.fourcc, "MJPG")
        self.assertEqual(args.profile, DEFAULT_PROFILE)
        self.assertEqual(PREVIEW_PROFILES[args.profile][:2], (UHD_WIDTH, UHD_HEIGHT))
        self.assertEqual(args.jpeg_quality, 80)
        self.assertEqual((FHD_WIDTH, FHD_HEIGHT), (1920, 1080))
        self.assertEqual(args.scan_limit, 6)

    def test_real_opencv_jpeg_preserves_4k_dimensions(self) -> None:
        frame = np.zeros((UHD_HEIGHT, UHD_WIDTH, 3), dtype=np.uint8)
        capture = FakeCapture(frames=[frame])
        stream, _cv2, _configured = make_stream(capture, cv2)
        stream.start(timeout=2.0)
        self.addCleanup(stream.stop)

        encoded = np.frombuffer(stream.latest_jpeg(), dtype=np.uint8)
        decoded = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        self.assertEqual(decoded.shape, (UHD_HEIGHT, UHD_WIDTH, 3))

    def test_stream_client_limit_is_bounded(self) -> None:
        manager = CameraManager(30, "MJPG", 88, 2, probe=lambda _limit: [])
        server = CameraHTTPServer(("127.0.0.1", 0), manager, max_stream_clients=1)
        try:
            self.assertTrue(server.acquire_stream())
            self.assertFalse(server.acquire_stream())
            server.release_stream()
        finally:
            server.server_close()
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
