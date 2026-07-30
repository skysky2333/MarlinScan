from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from v3se_printer.nikon import (
    CAPTURE_PROFILES,
    CAPTURE_EVENT_TIMEOUT_MS,
    CapturePair,
    DEFAULT_ISO,
    IMAGE_LARGE,
    IMAGE_SMALL,
    JPEG_BASIC,
    JPEG_FINE,
    MODEL,
    NEF_FINE,
    NikonJ4Camera,
    PROFILE_ANALYSIS,
    PROFILE_PREVIEW,
    PROFILE_RAW,
    RemoteCapturePair,
    RemoteFile,
    _terminate_ptpcamerad,
)


@dataclass(frozen=True)
class FakeRemotePath:
    folder: str
    name: str


@dataclass(frozen=True)
class FakeAbilities:
    model: str


class FakeWidget:
    def __init__(self, values: dict[str, str], choices: dict[str, tuple[str, ...]], name: str) -> None:
        self._values = values
        self._choices = choices
        self._name = name
        self.read_count = 0

    def get_value(self) -> str:
        self.read_count += 1
        return self._values[self._name]

    def get_choices(self):
        return iter(self._choices[self._name])

    def set_value(self, value: str) -> None:
        self._values[self._name] = value


class FakeConfig:
    def __init__(self, values: dict[str, str], choices: dict[str, tuple[str, ...]]) -> None:
        self.values = values
        self.choices = choices
        self.widgets: dict[str, FakeWidget] = {}

    def get_child_by_name(self, name: str) -> FakeWidget:
        widget = FakeWidget(self.values, self.choices, name)
        self.widgets[name] = widget
        return widget


class FakeFile:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def save(self, target: str) -> None:
        Path(target).write_bytes(self.payload)


class FakeCamera:
    def __init__(self, gp: "FakeGp", *, abilities_model: str = MODEL) -> None:
        self.gp = gp
        self.abilities_model = abilities_model
        self.init_count = 0
        self.exit_count = 0
        self.port_info = None
        self.abilities = None
        self.set_config_count = 0
        self.set_config_history: list[dict[str, str]] = []
        self.get_single_config_history: list[str] = []
        self.set_single_config_history: list[tuple[str, str]] = []
        self.get_config_count = 0
        self.capture_paths: deque[FakeRemotePath] = deque()
        self.events: deque[tuple[int, FakeRemotePath | None]] = deque()
        self.file_get_calls: list[tuple[str, str, int]] = []
        self.file_delete_calls: list[tuple[str, str]] = []
        self.event_timeouts: list[int] = []
        self.values = {
            "expprogram2": "P",
            "iso": "ISO Auto 800",
            "imagesize": "Large",
            "imagequality2": "JPEG Normal",
            "shutterspeed2": "1/60",
            "capturetarget": "Internal RAM",
        }
        self.choices = {
            "expprogram2": ("M", "P", "A", "S"),
            "iso": ("ISO Auto 800", "160", "200"),
            "imagesize": ("Small", "Medium", "Large"),
            "imagequality2": ("JPEG Normal", JPEG_FINE, JPEG_BASIC, NEF_FINE),
            "shutterspeed2": ("1/30", "1/60", "1/125"),
            "capturetarget": ("Internal RAM", "Memory card"),
        }
        self.reject: dict[str, str] = {}
        self.set_single_config_errors: dict[str, Exception] = {}
        self.capture_delay = 0.0
        self.active_captures = 0
        self.max_active_captures = 0

    def set_port_info(self, value: object) -> None:
        self.port_info = value

    def set_abilities(self, value: object) -> None:
        self.abilities = value

    def init(self) -> None:
        self.gp.order.append("init")
        self.init_count += 1

    def exit(self) -> None:
        self.exit_count += 1

    def get_abilities(self) -> FakeAbilities:
        return FakeAbilities(self.abilities_model)

    def get_config(self) -> FakeConfig:
        self.get_config_count += 1
        return FakeConfig(dict(self.values), self.choices)

    def set_config(self, config: FakeConfig) -> None:
        self.set_config_count += 1
        self.values = dict(config.values)
        self.values.update(self.reject)
        self.set_config_history.append(dict(self.values))

    def get_single_config(self, name: str) -> FakeWidget:
        self.get_single_config_history.append(name)
        return FakeWidget(dict(self.values), self.choices, name)

    def set_single_config(self, name: str, widget: FakeWidget) -> None:
        if widget._name != name:
            raise AssertionError((name, widget._name))
        requested = widget.get_value()
        self.set_single_config_history.append((name, requested))
        if name in self.set_single_config_errors:
            raise self.set_single_config_errors[name]
        self.values[name] = self.reject.get(name, requested)

    def capture(self, capture_type: int) -> FakeRemotePath:
        if capture_type != self.gp.GP_CAPTURE_IMAGE:
            raise AssertionError(capture_type)
        self.active_captures += 1
        self.max_active_captures = max(self.max_active_captures, self.active_captures)
        time.sleep(self.capture_delay)
        path = self.capture_paths.popleft()
        self.active_captures -= 1
        return path

    def wait_for_event(self, timeout_ms: int) -> tuple[int, FakeRemotePath | None]:
        self.event_timeouts.append(timeout_ms)
        return self.events.popleft()

    def file_get(self, folder: str, name: str, file_type: int) -> FakeFile:
        self.file_get_calls.append((folder, name, file_type))
        return FakeFile(name.encode("ascii"))

    def file_delete(self, folder: str, name: str) -> None:
        self.file_delete_calls.append((folder, name))


class FakeList:
    def __init__(self, values: dict[str, object]) -> None:
        self.values = values
        self.loaded = False

    def load(self) -> None:
        self.loaded = True

    def lookup_path(self, value: str) -> str:
        return value

    def lookup_model(self, value: str) -> str:
        return value

    def __getitem__(self, key: str) -> object:
        return self.values[key]


class FakeCameraType:
    detections: list[tuple[str, str]] = [(MODEL, "usb:001,002")]

    @classmethod
    def autodetect(cls) -> list[tuple[str, str]]:
        return list(cls.detections)


class FakeGp:
    GP_CAPTURE_IMAGE = 1
    GP_FILE_TYPE_NORMAL = 2
    GP_EVENT_TIMEOUT = 3
    GP_EVENT_FILE_ADDED = 4
    GP_EVENT_CAPTURE_COMPLETE = 5
    Camera = FakeCameraType

    def __init__(self) -> None:
        self.Camera.detections = [(MODEL, "usb:001,002")]
        self.order: list[str] = []
        self.camera = FakeCamera(self)

    def PortInfoList(self) -> FakeList:
        return FakeList({"usb:001,002": "port-info"})

    def CameraAbilitiesList(self) -> FakeList:
        return FakeList({MODEL: "j4-abilities"})


def connected_camera(
    *,
    abilities_model: str = MODEL,
    detections: list[tuple[str, str]] | None = None,
) -> tuple[NikonJ4Camera, FakeGp, FakeCamera, list[str]]:
    gp = FakeGp()
    gp.camera.abilities_model = abilities_model
    gp.Camera.detections = detections if detections is not None else [(MODEL, "usb:001,002")]
    order = gp.order

    def terminate() -> None:
        order.append("terminate")

    nikon = NikonJ4Camera(
        gp_module=gp,
        camera_factory=lambda: gp.camera,
        terminator=terminate,
        platform_name="darwin",
    )
    nikon.take_control()
    nikon.connect()
    return nikon, gp, gp.camera, order


class NikonLifecycleTests(unittest.TestCase):
    @patch("v3se_printer.nikon.subprocess.run")
    @patch("v3se_printer.nikon.os.getuid", return_value=501)
    def test_macos_takeover_forcefully_terminates_only_same_user_ptpcamerad(self, _getuid, run) -> None:
        run.return_value.returncode = 0
        _terminate_ptpcamerad()
        run.assert_called_once_with(
            ["pkill", "-9", "-u", "501", "-f", "^/usr/libexec/ptpcamerad$"],
            capture_output=True,
            text=True,
        )

    def test_take_control_connects_exact_j4_and_disconnects_once(self) -> None:
        camera, _gp, fake, order = connected_camera()

        camera.take_control()
        self.assertEqual(order, ["terminate", "terminate", "init"])
        self.assertEqual(fake.port_info, "port-info")
        self.assertEqual(fake.abilities, "j4-abilities")
        self.assertEqual(camera.status()["model"], MODEL)

        camera.disconnect()
        camera.disconnect()
        self.assertEqual(fake.exit_count, 1)
        self.assertFalse(camera.status()["connected"])
        self.assertFalse(camera.status()["control_taken"])

    def test_connect_requires_control_and_exactly_one_j4(self) -> None:
        gp = FakeGp()
        nikon = NikonJ4Camera(gp_module=gp, camera_factory=lambda: gp.camera, platform_name="linux")
        with self.assertRaisesRegex(RuntimeError, "Take control"):
            nikon.connect()

        nikon.take_control()
        gp.Camera.detections = [("Nikon J3", "usb:001,001")]
        with self.assertRaisesRegex(RuntimeError, "detected 0"):
            nikon.connect()
        self.assertEqual(gp.camera.init_count, 0)
        self.assertFalse(nikon.status()["control_taken"])

    def test_failed_connect_can_take_control_and_retry(self) -> None:
        gp = FakeGp()
        order = gp.order

        def terminate() -> None:
            order.append("terminate")

        nikon = NikonJ4Camera(
            gp_module=gp,
            camera_factory=lambda: gp.camera,
            terminator=terminate,
            platform_name="darwin",
        )
        nikon.take_control()
        gp.Camera.detections = []
        with self.assertRaises(RuntimeError):
            nikon.connect()
        gp.Camera.detections = [(MODEL, "usb:001,002")]
        nikon.take_control()
        nikon.connect()

        self.assertEqual(order, ["terminate", "terminate", "terminate", "init"])
        self.assertTrue(nikon.status()["connected"])

    def test_connected_abilities_must_still_be_j4(self) -> None:
        gp = FakeGp()
        gp.camera.abilities_model = "Nikon J3"
        nikon = NikonJ4Camera(gp_module=gp, camera_factory=lambda: gp.camera, platform_name="linux")
        nikon.take_control()
        with self.assertRaisesRegex(RuntimeError, "connected Nikon J3"):
            nikon.connect()
        self.assertEqual(gp.camera.exit_count, 1)


class NikonConfigurationTests(unittest.TestCase):
    def test_connect_applies_fixed_default_iso_and_exposes_camera_settings(self) -> None:
        nikon, _gp, camera, _order = connected_camera()

        self.assertEqual(camera.values["iso"], DEFAULT_ISO)
        self.assertEqual(
            nikon.settings(),
            {
                "iso": "160",
                "shutter": "1/60",
                "iso_choices": ("ISO Auto 800", "160", "200"),
                "shutter_choices": ("1/30", "1/60", "1/125"),
            },
        )
        self.assertEqual(nikon.iso_choices(), ("ISO Auto 800", "160", "200"))
        self.assertEqual(nikon.status()["iso"], "160")
        self.assertEqual(nikon.status()["shutter"], "1/60")
        self.assertIsNone(nikon.status()["configured_profile"])
        self.assertIsNone(nikon.status()["latest_capture_profile"])
        self.assertEqual(camera.values["expprogram2"], "P")
        self.assertEqual(camera.values["imagesize"], IMAGE_LARGE)
        self.assertEqual(camera.values["imagequality2"], "JPEG Normal")

    def test_global_iso_is_fixed_and_used_by_capture_configuration(self) -> None:
        nikon, _gp, camera, _order = connected_camera()

        settings = nikon.set_iso("200")
        self.assertEqual(settings["iso"], "200")
        self.assertEqual(nikon.status()["iso"], "200")
        nikon.configure("1/125", profile=PROFILE_RAW)

        self.assertEqual(camera.values["iso"], "200")
        with self.assertRaisesRegex(ValueError, "fixed camera value"):
            nikon.set_iso("ISO Auto 800")

    def test_configuration_writes_only_changed_properties_and_verifies_each(self) -> None:
        nikon, _gp, camera, _order = connected_camera()

        self.assertEqual(nikon.shutter_choices(), ("1/30", "1/60", "1/125"))
        set_count = camera.set_config_count
        single_start = len(camera.set_single_config_history)
        single_read_start = len(camera.get_single_config_history)
        get_count = camera.get_config_count
        actual = nikon.configure("1/125", profile=PROFILE_RAW)

        self.assertEqual(
            actual,
            {
                "expprogram2": "M",
                "iso": "160",
                "imagesize": IMAGE_LARGE,
                "imagequality2": NEF_FINE,
                "shutterspeed2": "1/125",
            },
        )
        self.assertEqual(camera.set_config_count, set_count)
        self.assertEqual(
            camera.set_single_config_history[single_start:],
            [
                ("expprogram2", "M"),
                ("imagequality2", NEF_FINE),
                ("shutterspeed2", "1/125"),
            ],
        )
        self.assertEqual(
            camera.get_single_config_history[single_read_start:],
            [
                "expprogram2",
                "iso",
                "imagesize",
                "imagequality2",
                "shutterspeed2",
                "expprogram2",
                "expprogram2",
                "iso",
                "imagesize",
                "imagequality2",
                "imagequality2",
                "shutterspeed2",
                "shutterspeed2",
            ],
        )
        self.assertEqual(camera.get_config_count, get_count + 1)
        self.assertEqual(nikon.status()["image_quality"], NEF_FINE)
        self.assertEqual(nikon.status()["configured_profile"], PROFILE_RAW)

    def test_capture_profiles_are_exact_and_configurable(self) -> None:
        self.assertEqual(
            CAPTURE_PROFILES,
            {
                PROFILE_PREVIEW: {"imagesize": IMAGE_SMALL, "imagequality2": JPEG_BASIC},
                PROFILE_ANALYSIS: {"imagesize": IMAGE_LARGE, "imagequality2": JPEG_FINE},
                PROFILE_RAW: {"imagesize": IMAGE_LARGE, "imagequality2": NEF_FINE},
            },
        )
        for profile, expected in CAPTURE_PROFILES.items():
            with self.subTest(profile=profile):
                nikon, _gp, camera, _order = connected_camera()
                nikon.configure("1/60", profile=profile)
                self.assertEqual(camera.values["imagesize"], expected["imagesize"])
                self.assertEqual(camera.values["imagequality2"], expected["imagequality2"])
                self.assertEqual(nikon.status()["configured_profile"], profile)

    def test_unavailable_choice_fails_before_set(self) -> None:
        nikon, _gp, camera, _order = connected_camera()
        set_count = camera.set_config_count
        single_set_count = len(camera.set_single_config_history)
        with self.assertRaisesRegex(ValueError, "not available"):
            nikon.configure("1/8000", profile=PROFILE_ANALYSIS)
        self.assertEqual(camera.set_config_count, set_count)
        self.assertEqual(len(camera.set_single_config_history), single_set_count)

        with self.assertRaisesRegex(ValueError, "Unknown capture profile"):
            nikon.configure("1/60", profile="normal")
        self.assertEqual(camera.set_config_count, set_count)
        self.assertEqual(len(camera.set_single_config_history), single_set_count)

    def test_rejected_setting_preserves_last_verified_configuration(self) -> None:
        nikon, _gp, camera, _order = connected_camera()
        nikon.configure("1/60", profile=PROFILE_ANALYSIS)
        camera.reject["imagequality2"] = JPEG_FINE
        history_start = len(camera.set_single_config_history)
        with self.assertRaisesRegex(RuntimeError, "Camera rejected imagequality2"):
            nikon.configure("1/125", profile=PROFILE_RAW)
        self.assertEqual(
            camera.set_single_config_history[history_start:],
            [("imagequality2", NEF_FINE)],
        )
        self.assertEqual(nikon.status()["image_quality"], JPEG_FINE)
        self.assertEqual(nikon.status()["configured_profile"], PROFILE_ANALYSIS)
        self.assertEqual(nikon.status()["shutter"], "1/60")

    def test_transient_configuration_failure_preserves_prior_profile_for_capture(self) -> None:
        nikon, gp, camera, _order = connected_camera()
        nikon.configure("1/60", profile=PROFILE_ANALYSIS)
        history_start = len(camera.set_single_config_history)
        camera.set_single_config_errors["imagequality2"] = RuntimeError(
            "[-2] Bad parameters"
        )
        with self.assertRaisesRegex(RuntimeError, "Bad parameters"):
            nikon.configure("1/125", profile=PROFILE_RAW)
        self.assertEqual(
            camera.set_single_config_history[history_start:],
            [("imagequality2", NEF_FINE)],
        )

        captured = FakeRemotePath("/store", "DSC_0001.JPG")
        camera.capture_paths.append(captured)
        camera.events.extend(
            [
                (gp.GP_EVENT_TIMEOUT, None),
                (gp.GP_EVENT_FILE_ADDED, captured),
                (gp.GP_EVENT_TIMEOUT, None),
            ]
        )
        with TemporaryDirectory() as directory:
            nikon.capture_calibration(Path(directory, "calibration.jpg"))

        self.assertEqual(nikon.status()["configured_profile"], PROFILE_ANALYSIS)
        self.assertEqual(nikon.status()["shutter"], "1/60")

    def test_later_configuration_failure_restores_verified_property_writes(self) -> None:
        nikon, _gp, camera, _order = connected_camera()
        nikon.configure("1/60", profile=PROFILE_ANALYSIS)
        history_start = len(camera.set_single_config_history)
        camera.set_single_config_errors["shutterspeed2"] = RuntimeError(
            "[-2] Bad parameters"
        )

        with self.assertRaisesRegex(RuntimeError, "Bad parameters"):
            nikon.configure("1/125", profile=PROFILE_RAW)

        self.assertEqual(
            camera.set_single_config_history[history_start:],
            [
                ("imagequality2", NEF_FINE),
                ("shutterspeed2", "1/125"),
                ("imagequality2", JPEG_FINE),
            ],
        )
        self.assertEqual(camera.values["imagequality2"], JPEG_FINE)
        self.assertEqual(camera.values["shutterspeed2"], "1/60")
        self.assertEqual(nikon.status()["configured_profile"], PROFILE_ANALYSIS)
        self.assertEqual(nikon.status()["shutter"], "1/60")

    def test_ambiguous_setting_failure_restores_the_property_that_was_sent(self) -> None:
        nikon, _gp, camera, _order = connected_camera()
        nikon.configure("1/60", profile=PROFILE_ANALYSIS)
        history_start = len(camera.set_single_config_history)
        set_single_config = camera.set_single_config

        def set_then_fail(name: str, widget: FakeWidget) -> None:
            requested = widget.get_value()
            if name != "shutterspeed2" or requested != "1/125":
                set_single_config(name, widget)
                return
            camera.set_single_config_history.append((name, requested))
            camera.values[name] = requested
            raise RuntimeError("transport failed after camera write")

        camera.set_single_config = set_then_fail
        with self.assertRaisesRegex(RuntimeError, "transport failed"):
            nikon.configure("1/125", profile=PROFILE_RAW)

        self.assertEqual(
            camera.set_single_config_history[history_start:],
            [
                ("imagequality2", NEF_FINE),
                ("shutterspeed2", "1/125"),
                ("shutterspeed2", "1/60"),
                ("imagequality2", JPEG_FINE),
            ],
        )
        self.assertEqual(camera.values["imagequality2"], JPEG_FINE)
        self.assertEqual(camera.values["shutterspeed2"], "1/60")
        self.assertEqual(nikon.status()["configured_profile"], PROFILE_ANALYSIS)
        self.assertEqual(nikon.status()["shutter"], "1/60")


class NikonCaptureTests(unittest.TestCase):
    def test_capture_methods_require_their_exact_profiles(self) -> None:
        nikon, _gp, camera, _order = connected_camera()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            nikon.configure("1/60", profile=PROFILE_RAW)
            with self.assertRaisesRegex(RuntimeError, "profile='analysis'"):
                nikon.capture_calibration(root / "calibration.jpg")

            nikon.configure("1/60", profile=PROFILE_ANALYSIS)
            with self.assertRaisesRegex(RuntimeError, "profile='raw'"):
                nikon.capture_scan(root / "tile.jpg", root / "tile.nef")
        self.assertEqual(len(camera.capture_paths), 0)

    def test_preview_uses_small_basic_jpeg_then_restores_full_capture_settings(self) -> None:
        nikon, gp, camera, _order = connected_camera()
        nikon.set_iso("200")
        nikon.configure("1/125", profile=PROFILE_RAW)
        history_start = len(camera.set_single_config_history)
        set_single_config = camera.set_single_config

        def reject_invalid_combination(name: str, widget: FakeWidget) -> None:
            requested = widget.get_value()
            size = requested if name == "imagesize" else camera.values["imagesize"]
            quality = requested if name == "imagequality2" else camera.values["imagequality2"]
            if size == IMAGE_SMALL and quality == NEF_FINE:
                raise RuntimeError("[-2] Bad parameters")
            set_single_config(name, widget)

        camera.set_single_config = reject_invalid_combination
        captured = FakeRemotePath("/store", "DSC_0001.JPG")
        camera.capture_paths.append(captured)
        camera.events.extend(
            [
                (gp.GP_EVENT_TIMEOUT, None),
                (gp.GP_EVENT_FILE_ADDED, captured),
                (gp.GP_EVENT_TIMEOUT, None),
            ]
        )

        with TemporaryDirectory() as directory:
            target = Path(directory, "preview.jpg")
            self.assertEqual(nikon.capture_preview(target), target)
            self.assertEqual(target.read_bytes(), b"DSC_0001.JPG")

        self.assertEqual(
            camera.set_single_config_history[history_start:],
            [
                ("imagequality2", JPEG_BASIC),
                ("imagesize", IMAGE_SMALL),
                ("imagesize", IMAGE_LARGE),
                ("imagequality2", NEF_FINE),
            ],
        )
        self.assertEqual(nikon.status()["image_quality"], NEF_FINE)
        self.assertEqual(nikon.status()["configured_profile"], PROFILE_RAW)
        self.assertEqual(nikon.status()["latest_capture_profile"], PROFILE_PREVIEW)
        self.assertEqual(camera.file_delete_calls, [("/store", "DSC_0001.JPG")])

    def test_preview_restores_capture_settings_when_capture_fails(self) -> None:
        nikon, gp, camera, _order = connected_camera()
        nikon.configure("1/60", profile=PROFILE_ANALYSIS)
        history_start = len(camera.set_single_config_history)
        camera.events.append((gp.GP_EVENT_TIMEOUT, None))

        with TemporaryDirectory() as directory:
            with self.assertRaises(IndexError):
                nikon.capture_preview(Path(directory, "preview.jpg"))

        self.assertEqual(
            camera.set_single_config_history[history_start:],
            [
                ("imagequality2", JPEG_BASIC),
                ("imagesize", IMAGE_SMALL),
                ("imagesize", IMAGE_LARGE),
                ("imagequality2", JPEG_FINE),
            ],
        )
        self.assertEqual(nikon.status()["configured_profile"], PROFILE_ANALYSIS)
        self.assertIsNone(nikon.status()["latest_capture_profile"])

    def test_calibration_drains_stale_events_and_saves_exactly_one_jpeg(self) -> None:
        nikon, gp, camera, _order = connected_camera()
        nikon.configure("1/60", profile=PROFILE_ANALYSIS)
        captured = FakeRemotePath("/store", "DSC_0002.JPG")
        camera.capture_paths.append(captured)
        camera.events.extend(
            [
                (gp.GP_EVENT_FILE_ADDED, FakeRemotePath("/store", "DSC_0001.JPG")),
                (gp.GP_EVENT_TIMEOUT, None),
                (gp.GP_EVENT_FILE_ADDED, captured),
                (gp.GP_EVENT_CAPTURE_COMPLETE, None),
                (gp.GP_EVENT_TIMEOUT, None),
            ]
        )

        with TemporaryDirectory() as directory:
            target = Path(directory, "calibration.jpg")
            self.assertEqual(nikon.capture_calibration(target), target)
            self.assertEqual(target.read_bytes(), b"DSC_0002.JPG")
            self.assertEqual(nikon.latest_jpeg_path, target)
            self.assertEqual(nikon.status()["latest_jpeg_path"], str(target))
            self.assertEqual(nikon.status()["latest_capture_profile"], PROFILE_ANALYSIS)

        self.assertEqual(camera.file_get_calls, [("/store", "DSC_0002.JPG", gp.GP_FILE_TYPE_NORMAL)])
        self.assertEqual(camera.file_delete_calls, [("/store", "DSC_0002.JPG")])
        self.assertEqual(
            camera.event_timeouts,
            [1, 1, 1, 1, 1],
        )

    def test_transfer_failures_delete_every_returned_camera_file(self) -> None:
        for mode in ("preview", "calibration", "scan"):
            with self.subTest(mode=mode):
                nikon, gp, camera, _order = connected_camera()
                nikon.configure("1/125", profile=PROFILE_RAW if mode == "scan" else PROFILE_ANALYSIS)
                jpeg = FakeRemotePath("/store", "DSC_0042.JPG")
                camera.capture_paths.append(jpeg)
                camera.events.append((gp.GP_EVENT_TIMEOUT, None))
                if mode == "scan":
                    camera.events.append(
                        (gp.GP_EVENT_FILE_ADDED, FakeRemotePath("/store", "DSC_0042.NEF"))
                    )
                camera.events.append((gp.GP_EVENT_TIMEOUT, None))
                camera.file_get = lambda *_args: (_ for _ in ()).throw(RuntimeError("transfer failed"))

                with TemporaryDirectory() as directory, self.assertRaisesRegex(RuntimeError, "transfer failed"):
                    root = Path(directory)
                    if mode == "preview":
                        nikon.capture_preview(root / "preview.jpg")
                    elif mode == "calibration":
                        nikon.capture_calibration(root / "calibration.jpg")
                    else:
                        nikon.capture_scan(root / "tile.jpg", root / "tile.nef")

                expected = [("/store", "DSC_0042.JPG")]
                if mode == "scan":
                    expected.append(("/store", "DSC_0042.NEF"))
                self.assertEqual(camera.file_delete_calls, expected)

    def test_scan_pair_is_order_independent_and_deduplicated(self) -> None:
        for first_name, added_name in (
            ("DSC_0042.NEF", "DSC_0042.JPG"),
            ("DSC_0042.JPG", "DSC_0042.NEF"),
        ):
            with self.subTest(first=first_name):
                nikon, gp, camera, _order = connected_camera()
                nikon.configure("1/125", profile=PROFILE_RAW)
                first = FakeRemotePath("/store", first_name)
                added = FakeRemotePath("/store", added_name)
                camera.capture_paths.append(first)
                camera.events.extend(
                    [
                        (gp.GP_EVENT_TIMEOUT, None),
                        (gp.GP_EVENT_FILE_ADDED, added),
                        (gp.GP_EVENT_FILE_ADDED, first),
                        (gp.GP_EVENT_TIMEOUT, None),
                    ]
                )

                with TemporaryDirectory() as directory:
                    jpeg = Path(directory, "tile.jpg")
                    nef = Path(directory, "tile.nef")
                    pair = nikon.capture_scan(jpeg, nef)
                    self.assertEqual(pair, CapturePair(jpeg=jpeg, nef=nef))
                    self.assertEqual(jpeg.read_bytes(), b"DSC_0042.JPG")
                    self.assertEqual(nef.read_bytes(), b"DSC_0042.NEF")
                    self.assertEqual(nikon.status()["latest_capture_profile"], PROFILE_RAW)
                self.assertEqual(
                    camera.file_delete_calls,
                    [("/store", "DSC_0042.JPG"), ("/store", "DSC_0042.NEF")],
                )
                self.assertEqual(
                    [name for _folder, name, _file_type in camera.file_get_calls],
                    ["DSC_0042.JPG", "DSC_0042.NEF"],
                )
                self.assertEqual(camera.event_timeouts, [1, CAPTURE_EVENT_TIMEOUT_MS, 1, 1])

    def test_scan_jpeg_is_visible_while_nef_download_continues(self) -> None:
        nikon, gp, camera, _order = connected_camera()
        nikon.configure("1/125", profile=PROFILE_RAW)
        jpeg_source = FakeRemotePath("/store", "DSC_0042.JPG")
        nef_source = FakeRemotePath("/store", "DSC_0042.NEF")
        camera.capture_paths.append(jpeg_source)
        camera.events.extend(
            [
                (gp.GP_EVENT_TIMEOUT, None),
                (gp.GP_EVENT_FILE_ADDED, nef_source),
                (gp.GP_EVENT_TIMEOUT, None),
            ]
        )
        nef_started = threading.Event()
        release_nef = threading.Event()
        file_get = camera.file_get

        def blocking_file_get(folder: str, name: str, file_type: int) -> FakeFile:
            if name.endswith(".NEF"):
                nef_started.set()
                release_nef.wait(1.0)
            return file_get(folder, name, file_type)

        camera.file_get = blocking_file_get
        with TemporaryDirectory() as directory:
            jpeg = Path(directory, "tile.jpg")
            nef = Path(directory, "tile.nef")
            with ThreadPoolExecutor(max_workers=1) as executor:
                result = executor.submit(nikon.capture_scan, jpeg, nef)
                self.assertTrue(nef_started.wait(1.0))
                self.assertEqual(nikon.status()["latest_jpeg_path"], str(jpeg))
                self.assertEqual(nikon.latest_jpeg_path, jpeg)
                release_nef.set()
                self.assertEqual(result.result(), CapturePair(jpeg, nef))

    def test_deferred_scan_stays_on_card_until_explicit_download(self) -> None:
        nikon, gp, camera, _order = connected_camera()
        nikon.configure("1/125", profile=PROFILE_RAW)
        jpeg_source = FakeRemotePath("/card", "DSC_0042.JPG")
        nef_source = FakeRemotePath("/card", "DSC_0042.NEF")
        camera.capture_paths.append(jpeg_source)
        camera.events.extend(
            [
                (gp.GP_EVENT_TIMEOUT, None),
                (gp.GP_EVENT_FILE_ADDED, nef_source),
                (gp.GP_EVENT_TIMEOUT, None),
            ]
        )

        original = nikon.use_camera_storage()
        sources = nikon.capture_scan_to_camera()
        self.assertEqual(original, "Internal RAM")
        self.assertEqual(camera.values["capturetarget"], "Memory card")
        self.assertEqual(
            sources,
            RemoteCapturePair(
                RemoteFile("/card", "DSC_0042.JPG"),
                RemoteFile("/card", "DSC_0042.NEF"),
            ),
        )
        self.assertEqual(camera.file_get_calls, [])
        self.assertEqual(camera.file_delete_calls, [])

        with TemporaryDirectory() as directory:
            jpeg = Path(directory, "tile.jpg")
            nef = Path(directory, "tile.nef")
            self.assertEqual(nikon.download_scan(sources, jpeg, nef), CapturePair(jpeg, nef))
            self.assertEqual(jpeg.read_bytes(), b"DSC_0042.JPG")
            self.assertEqual(nef.read_bytes(), b"DSC_0042.NEF")
        self.assertEqual(camera.file_delete_calls, [])
        nikon.delete_scan(sources)
        nikon.restore_capture_storage(original)
        self.assertEqual(camera.values["capturetarget"], "Internal RAM")
        self.assertEqual(
            [name for _folder, name, _file_type in camera.file_get_calls],
            ["DSC_0042.JPG", "DSC_0042.NEF"],
        )
        self.assertEqual(
            camera.file_delete_calls,
            [("/card", "DSC_0042.JPG"), ("/card", "DSC_0042.NEF")],
        )

    def test_camera_storage_selects_the_named_card_target(self) -> None:
        nikon, _gp, camera, _order = connected_camera()
        camera.choices["capturetarget"] = ("Memory card", "Internal RAM")

        original = nikon.use_camera_storage()

        self.assertEqual(original, "Internal RAM")
        self.assertEqual(camera.values["capturetarget"], "Memory card")

        nikon.restore_capture_storage(original)
        camera.choices["capturetarget"] = ("Internal RAM", "USB storage")
        with self.assertRaisesRegex(RuntimeError, "memory-card capture is unavailable"):
            nikon.use_camera_storage()

    def test_explicit_scan_delete_attempts_both_files_when_one_fails(self) -> None:
        nikon, _gp, camera, _order = connected_camera()
        sources = RemoteCapturePair(
            RemoteFile("/card", "DSC_0042.JPG"),
            RemoteFile("/card", "DSC_0042.NEF"),
        )
        calls: list[tuple[str, str]] = []

        def delete(folder: str, name: str) -> None:
            calls.append((folder, name))
            if name.endswith(".JPG"):
                raise RuntimeError("delete failed")

        camera.file_delete = delete
        with self.assertRaisesRegex(RuntimeError, "delete failed"):
            nikon.delete_scan(sources)
        self.assertEqual(
            calls,
            [("/card", "DSC_0042.JPG"), ("/card", "DSC_0042.NEF")],
        )

    def test_camera_storage_selection_failure_restores_original_target(self) -> None:
        nikon, _gp, camera, _order = connected_camera()
        writes: list[str] = []

        def write_settings(_camera: object, expected: dict[str, str]):
            target = expected["capturetarget"]
            writes.append(target)
            camera.values["capturetarget"] = target
            if target == "Memory card":
                raise RuntimeError("capture target readback failed")
            return camera.get_config(), expected

        with patch.object(nikon, "_write_settings", side_effect=write_settings):
            with self.assertRaisesRegex(RuntimeError, "capture target readback failed"):
                nikon.use_camera_storage()
        self.assertEqual(writes, ["Memory card", "Internal RAM"])
        self.assertEqual(camera.values["capturetarget"], "Internal RAM")

    def test_incomplete_mismatched_or_ambiguous_pair_fails(self) -> None:
        cases = (
            ("missing", ["DSC_0001.JPG"]),
            ("mismatched", ["DSC_0001.JPG", "DSC_0002.NEF"]),
            ("ambiguous", ["DSC_0001.JPG", "DSC_0001.NEF", "DSC_0001.XMP"]),
        )
        for label, names in cases:
            with self.subTest(case=label):
                nikon, gp, camera, _order = connected_camera()
                nikon.configure("1/60", profile=PROFILE_RAW)
                camera.capture_paths.append(FakeRemotePath("/store", names[0]))
                camera.events.append((gp.GP_EVENT_TIMEOUT, None))
                camera.events.extend(
                    (gp.GP_EVENT_FILE_ADDED, FakeRemotePath("/store", name))
                    for name in names[1:]
                )
                camera.events.append((gp.GP_EVENT_TIMEOUT, None))
                with TemporaryDirectory() as directory:
                    with self.assertRaises(RuntimeError):
                        nikon.capture_scan(
                            Path(directory, "tile.jpg"),
                            Path(directory, "tile.nef"),
                        )
                self.assertEqual(
                    set(camera.file_delete_calls),
                    {
                        ("/store", name)
                        for name in names
                        if Path(name).suffix.casefold() in {".jpg", ".jpeg", ".nef"}
                    },
                )

    def test_destination_validation_happens_before_capture(self) -> None:
        nikon, _gp, camera, _order = connected_camera()
        nikon.configure("1/60", profile=PROFILE_RAW)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "stems must match"):
                nikon.capture_scan(root / "a.jpg", root / "b.nef")
            existing = root / "tile.jpg"
            existing.write_bytes(b"existing")
            with self.assertRaises(FileExistsError):
                nikon.capture_scan(existing, root / "tile.nef")
        self.assertEqual(len(camera.capture_paths), 0)

    def test_camera_operations_are_serialized(self) -> None:
        nikon, gp, camera, _order = connected_camera()
        nikon.configure("1/60", profile=PROFILE_ANALYSIS)
        camera.capture_delay = 0.02
        camera.capture_paths.extend(
            [
                FakeRemotePath("/store", "DSC_0001.JPG"),
                FakeRemotePath("/store", "DSC_0002.JPG"),
            ]
        )
        camera.events.extend(
            [
                (gp.GP_EVENT_TIMEOUT, None),
                (gp.GP_EVENT_TIMEOUT, None),
                (gp.GP_EVENT_TIMEOUT, None),
                (gp.GP_EVENT_TIMEOUT, None),
            ]
        )
        with TemporaryDirectory() as directory:
            targets = [Path(directory, "one.jpg"), Path(directory, "two.jpg")]
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(nikon.capture_calibration, targets))
            self.assertEqual(results, targets)
        self.assertEqual(camera.max_active_captures, 1)


if __name__ == "__main__":
    unittest.main()
