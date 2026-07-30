from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from importlib import import_module
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Callable


MODEL = "Nikon J4"
DEFAULT_ISO = "160"
JPEG_BASIC = "JPEG Basic"
JPEG_FINE = "JPEG Fine"
NEF_FINE = "NEF+Fine"
IMAGE_LARGE = "Large"
IMAGE_SMALL = "Small"
PROFILE_PREVIEW = "preview"
PROFILE_ANALYSIS = "analysis"
PROFILE_RAW = "raw"
CAPTURE_PROFILES = {
    PROFILE_PREVIEW: {"imagesize": IMAGE_SMALL, "imagequality2": JPEG_BASIC},
    PROFILE_ANALYSIS: {"imagesize": IMAGE_LARGE, "imagequality2": JPEG_FINE},
    PROFILE_RAW: {"imagesize": IMAGE_LARGE, "imagequality2": NEF_FINE},
}
CAPTURE_EVENT_TIMEOUT_MS = 2000


@dataclass(frozen=True)
class CapturePair:
    jpeg: Path
    nef: Path


@dataclass(frozen=True)
class RemoteFile:
    folder: str
    name: str


@dataclass(frozen=True)
class RemoteCapturePair:
    jpeg: RemoteFile
    nef: RemoteFile


def _terminate_ptpcamerad() -> None:
    result = subprocess.run(
        ["pkill", "-9", "-u", str(os.getuid()), "-f", "^/usr/libexec/ptpcamerad$"],
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.strip() or "Failed to terminate ptpcamerad")


class NikonJ4Camera:
    def __init__(
        self,
        *,
        gp_module: object | None = None,
        camera_factory: Callable[[], object] | None = None,
        terminator: Callable[[], None] = _terminate_ptpcamerad,
        platform_name: str = sys.platform,
    ) -> None:
        self._gp = gp_module if gp_module is not None else import_module("gphoto2")
        self._camera_factory = camera_factory or self._gp.Camera
        self._terminator = terminator
        self._platform_name = platform_name
        self._camera_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._control_taken = False
        self._camera: object | None = None
        self._model: str | None = None
        self._image_quality: str | None = None
        self._configured_profile: str | None = None
        self._latest_capture_profile: str | None = None
        self._iso = DEFAULT_ISO
        self._iso_choices: tuple[str, ...] = ()
        self._shutter: str | None = None
        self._shutter_choices: tuple[str, ...] = ()
        self._latest_jpeg: Path | None = None

    def take_control(self) -> dict[str, object]:
        with self._camera_lock:
            with self._state_lock:
                connected = self._camera is not None
            if connected:
                return self._status()
            if self._platform_name == "darwin":
                self._terminator()
            with self._state_lock:
                self._control_taken = True
            return self._status()

    def connect(self) -> dict[str, object]:
        with self._camera_lock:
            with self._state_lock:
                control_taken = self._control_taken
                connected = self._camera is not None
                iso = self._iso
            if not control_taken:
                raise RuntimeError("Take control of the Nikon before connecting")
            if connected:
                raise RuntimeError("Nikon J4 is already connected")

            camera = None
            initialized = False
            try:
                matches = [
                    port
                    for model, port in self._gp.Camera.autodetect()
                    if model == MODEL
                ]
                if len(matches) != 1:
                    raise RuntimeError(f"Expected exactly one {MODEL}; detected {len(matches)}")

                port_info_list = self._gp.PortInfoList()
                port_info_list.load()
                abilities_list = self._gp.CameraAbilitiesList()
                abilities_list.load()
                camera = self._camera_factory()
                camera.set_port_info(port_info_list[port_info_list.lookup_path(matches[0])])
                camera.set_abilities(abilities_list[abilities_list.lookup_model(MODEL)])
                if self._platform_name == "darwin":
                    self._terminator()
                camera.init()
                initialized = True
                model = camera.get_abilities().model
                if model != MODEL:
                    raise RuntimeError(f"Expected {MODEL}; connected {model}")
                self._validate_iso(iso)
                config, _actual = self._write_settings(camera, {"iso": iso})
                self._cache_settings(config)
            except Exception:
                if initialized:
                    camera.exit()
                with self._state_lock:
                    self._control_taken = False
                raise

            with self._state_lock:
                self._camera = camera
                self._model = model
            return self._status()

    def shutter_choices(self) -> tuple[str, ...]:
        with self._camera_lock:
            return self._read_settings(self._connected_camera())["shutter_choices"]

    def iso_choices(self) -> tuple[str, ...]:
        with self._camera_lock:
            return self._read_settings(self._connected_camera())["iso_choices"]

    def settings(self) -> dict[str, object]:
        with self._camera_lock:
            return self._read_settings(self._connected_camera())

    def set_iso(self, iso: str) -> dict[str, object]:
        with self._camera_lock:
            self._validate_iso(iso)
            camera = self._connected_camera()
            config, actual = self._write_settings(camera, {"iso": iso})
            with self._state_lock:
                self._iso = actual["iso"]
            self._cache_settings(config)
            return self._settings(config)

    def configure(self, shutter: str, *, profile: str) -> dict[str, str]:
        with self._camera_lock:
            camera = self._connected_camera()
            with self._state_lock:
                iso = self._iso
            profile_settings = self._ordered_profile_settings(self._profile_settings(profile))
            expected = {
                "expprogram2": "M",
                "iso": iso,
                **profile_settings,
                "shutterspeed2": shutter,
            }
            config, actual = self._write_settings(camera, expected)
            self._cache_settings(config)
            with self._state_lock:
                self._image_quality = expected["imagequality2"]
                self._configured_profile = profile
                self._shutter = shutter
            return actual

    def capture_preview(self, jpeg_path: str | Path) -> Path:
        with self._camera_lock:
            camera = self._connected_camera()
            target = self._target(jpeg_path, {".jpg", ".jpeg"})
            preview_settings = self._ordered_profile_settings(self._profile_settings(PROFILE_PREVIEW))
            config = camera.get_config()
            original = {
                name: config.get_child_by_name(name).get_value()
                for name in preview_settings
            }
            with self._state_lock:
                original_quality = self._image_quality
                original_profile = self._configured_profile
            try:
                self._write_settings(camera, preview_settings)
                with self._state_lock:
                    self._image_quality = JPEG_BASIC
                    self._configured_profile = PROFILE_PREVIEW
                remote_paths = self._capture_paths(camera, 1)
                if len(remote_paths) != 1 or self._extension(remote_paths[0]) not in {".jpg", ".jpeg"}:
                    raise RuntimeError("Preview capture must produce exactly one JPEG")
                source = remote_paths[0]
                with ExitStack() as cleanup:
                    cleanup.callback(camera.file_delete, source.folder, source.name)
                    self._download(camera, source, target)
            finally:
                self._write_settings(camera, self._ordered_profile_settings(original))
                with self._state_lock:
                    self._image_quality = original_quality
                    self._configured_profile = original_profile
            with self._state_lock:
                self._latest_jpeg = target
                self._latest_capture_profile = PROFILE_PREVIEW
            return target

    def capture_calibration(self, jpeg_path: str | Path) -> Path:
        with self._camera_lock:
            camera = self._connected_camera()
            self._require_profile(PROFILE_ANALYSIS)
            target = self._target(jpeg_path, {".jpg", ".jpeg"})
            remote_paths = self._capture_paths(camera, 1)
            if len(remote_paths) != 1 or self._extension(remote_paths[0]) not in {".jpg", ".jpeg"}:
                raise RuntimeError("Calibration capture must produce exactly one JPEG")
            source = remote_paths[0]
            with ExitStack() as cleanup:
                cleanup.callback(camera.file_delete, source.folder, source.name)
                self._download(camera, source, target)
            with self._state_lock:
                self._latest_jpeg = target
                self._latest_capture_profile = PROFILE_ANALYSIS
            return target

    def capture_scan(self, jpeg_path: str | Path, nef_path: str | Path) -> CapturePair:
        with self._camera_lock:
            camera = self._connected_camera()
            self._require_profile(PROFILE_RAW)
            jpeg_target = self._target(jpeg_path, {".jpg", ".jpeg"})
            nef_target = self._target(nef_path, {".nef"})
            if jpeg_target.stem != nef_target.stem:
                raise ValueError("JPEG and NEF destination stems must match")
            sources = self._capture_scan_sources(camera)
            with ExitStack() as cleanup:
                cleanup.callback(self._delete_scan, camera, sources)
                return self._download_scan(camera, sources, jpeg_target, nef_target)

    def use_camera_storage(self) -> str:
        with self._camera_lock:
            camera = self._connected_camera()
            config = camera.get_config()
            target = config.get_child_by_name("capturetarget")
            choices = tuple(target.get_choices())
            if "Memory card" not in choices:
                raise RuntimeError("Nikon memory-card capture is unavailable")
            original = target.get_value()
            with ExitStack() as rollback:
                rollback.callback(self._write_settings, camera, {"capturetarget": original})
                self._write_settings(camera, {"capturetarget": "Memory card"})
                rollback.pop_all()
            return original

    def restore_capture_storage(self, target: str) -> None:
        with self._camera_lock:
            self._write_settings(self._connected_camera(), {"capturetarget": target})

    def capture_scan_to_camera(self) -> RemoteCapturePair:
        with self._camera_lock:
            camera = self._connected_camera()
            self._require_profile(PROFILE_RAW)
            return self._capture_scan_sources(camera)

    def download_scan(
        self,
        sources: RemoteCapturePair,
        jpeg_path: str | Path,
        nef_path: str | Path,
    ) -> CapturePair:
        with self._camera_lock:
            camera = self._connected_camera()
            self._require_profile(PROFILE_RAW)
            jpeg_target = self._target(jpeg_path, {".jpg", ".jpeg"})
            nef_target = self._target(nef_path, {".nef"})
            if jpeg_target.stem != nef_target.stem:
                raise ValueError("JPEG and NEF destination stems must match")
            return self._download_scan(camera, sources, jpeg_target, nef_target)

    def delete_scan(self, sources: RemoteCapturePair) -> None:
        with self._camera_lock:
            self._delete_scan(self._connected_camera(), sources)

    def status(self) -> dict[str, object]:
        with self._state_lock:
            return self._status()

    @property
    def latest_jpeg_path(self) -> Path | None:
        with self._state_lock:
            return self._latest_jpeg

    def disconnect(self) -> None:
        with self._camera_lock:
            camera = self._connected_camera() if self.status()["connected"] else None
            if camera is not None:
                camera.exit()
            with self._state_lock:
                self._camera = None
                self._model = None
                self._image_quality = None
                self._configured_profile = None
                self._shutter = None
                self._iso_choices = ()
                self._shutter_choices = ()
                self._control_taken = False

    def _connected_camera(self) -> object:
        with self._state_lock:
            camera = self._camera
        if camera is None:
            raise RuntimeError("Nikon J4 is not connected")
        return camera

    def _require_profile(self, expected: str) -> None:
        with self._state_lock:
            actual = self._configured_profile
        if actual != expected:
            raise RuntimeError(f"Configure profile={expected!r} before capture")

    @staticmethod
    def _profile_settings(profile: str) -> dict[str, str]:
        if profile not in CAPTURE_PROFILES:
            raise ValueError(f"Unknown capture profile: {profile!r}")
        return CAPTURE_PROFILES[profile]

    @staticmethod
    def _ordered_profile_settings(settings: dict[str, str]) -> dict[str, str]:
        if settings["imagesize"] == IMAGE_SMALL:
            return {"imagequality2": settings["imagequality2"], "imagesize": settings["imagesize"]}
        return {"imagesize": settings["imagesize"], "imagequality2": settings["imagequality2"]}

    def _read_settings(self, camera: object) -> dict[str, object]:
        config = camera.get_config()
        settings = self._settings(config)
        with self._state_lock:
            iso = self._iso
        if settings["iso"] != iso:
            raise RuntimeError(
                f"Camera ISO changed from {iso!r} to {settings['iso']!r}"
            )
        self._cache_settings(config)
        return settings

    def _cache_settings(self, config: object) -> None:
        settings = self._settings(config)
        with self._state_lock:
            self._iso_choices = settings["iso_choices"]
            self._shutter = settings["shutter"]
            self._shutter_choices = settings["shutter_choices"]

    @staticmethod
    def _settings(config: object) -> dict[str, object]:
        iso = config.get_child_by_name("iso")
        shutter = config.get_child_by_name("shutterspeed2")
        return {
            "iso": iso.get_value(),
            "shutter": shutter.get_value(),
            "iso_choices": tuple(iso.get_choices()),
            "shutter_choices": tuple(shutter.get_choices()),
        }

    @staticmethod
    def _validate_iso(iso: str) -> None:
        if not iso or "auto" in iso.casefold():
            raise ValueError("ISO must be a fixed camera value")

    @staticmethod
    def _restore_setting(camera: object, name: str, value: str) -> None:
        widget = camera.get_single_config(name)
        if value not in tuple(widget.get_choices()):
            raise ValueError(f"{value!r} is not available for {name}")
        if widget.get_value() == value:
            return
        widget.set_value(value)
        camera.set_single_config(name, widget)
        actual = camera.get_single_config(name).get_value()
        if actual != value:
            raise RuntimeError(
                f"Camera rollback rejected {name}={value!r}; read back {actual!r}"
            )

    @staticmethod
    def _write_settings(
        camera: object,
        expected: dict[str, str],
    ) -> tuple[object, dict[str, str]]:
        for name, value in expected.items():
            choices = tuple(camera.get_single_config(name).get_choices())
            if value not in choices:
                raise ValueError(f"{value!r} is not available for {name}")

        with ExitStack() as rollback:
            for name, value in expected.items():
                widget = camera.get_single_config(name)
                current = widget.get_value()
                if value not in tuple(widget.get_choices()):
                    raise ValueError(f"{value!r} is not available for {name}")
                if current == value:
                    continue
                rollback.callback(NikonJ4Camera._restore_setting, camera, name, current)
                widget.set_value(value)
                camera.set_single_config(name, widget)
                actual_value = camera.get_single_config(name).get_value()
                if actual_value != value:
                    raise RuntimeError(
                        f"Camera rejected {name}={value!r}; read back {actual_value!r}"
                    )
            readback = camera.get_config()
            actual = {
                name: readback.get_child_by_name(name).get_value()
                for name in expected
            }
            for name, value in expected.items():
                if actual[name] != value:
                    raise RuntimeError(
                        f"Camera rejected {name}={value!r}; read back {actual[name]!r}"
                    )
            rollback.pop_all()
            return readback, actual

    def _capture_paths(self, camera: object, expected_count: int) -> list[object]:
        self._drain_events(camera)
        first = camera.capture(self._gp.GP_CAPTURE_IMAGE)
        paths = {(first.folder, first.name): first}
        while True:
            timeout_ms = CAPTURE_EVENT_TIMEOUT_MS if len(paths) < expected_count else 1
            event_type, event_data = camera.wait_for_event(timeout_ms)
            if event_type == self._gp.GP_EVENT_TIMEOUT:
                return list(paths.values())
            if event_type == self._gp.GP_EVENT_FILE_ADDED:
                paths.setdefault((event_data.folder, event_data.name), event_data)

    def _drain_events(self, camera: object) -> None:
        while camera.wait_for_event(1)[0] != self._gp.GP_EVENT_TIMEOUT:
            pass

    def _download(self, camera: object, source: object, target: Path) -> None:
        camera.file_get(
            source.folder,
            source.name,
            self._gp.GP_FILE_TYPE_NORMAL,
        ).save(str(target))
        if not target.is_file() or target.stat().st_size == 0:
            raise RuntimeError(f"Camera downloaded an empty file: {target.name}")

    def _capture_scan_sources(self, camera: object) -> RemoteCapturePair:
        remote_paths = self._capture_paths(camera, 2)
        with ExitStack() as cleanup:
            for path in remote_paths:
                if self._extension(path) in {".jpg", ".jpeg", ".nef"}:
                    cleanup.callback(camera.file_delete, path.folder, path.name)
            jpeg_sources = [path for path in remote_paths if self._extension(path) in {".jpg", ".jpeg"}]
            nef_sources = [path for path in remote_paths if self._extension(path) == ".nef"]
            if len(remote_paths) != 2 or len(jpeg_sources) != 1 or len(nef_sources) != 1:
                raise RuntimeError("Scan capture must produce exactly one JPEG and one NEF")
            if self._stem(jpeg_sources[0]) != self._stem(nef_sources[0]):
                raise RuntimeError("Captured JPEG and NEF stems do not match")
            pair = RemoteCapturePair(
                RemoteFile(jpeg_sources[0].folder, jpeg_sources[0].name),
                RemoteFile(nef_sources[0].folder, nef_sources[0].name),
            )
            cleanup.pop_all()
            return pair

    def _download_scan(
        self,
        camera: object,
        sources: RemoteCapturePair,
        jpeg_target: Path,
        nef_target: Path,
    ) -> CapturePair:
        if self._stem(sources.jpeg) != self._stem(sources.nef):
            raise RuntimeError("Captured JPEG and NEF stems do not match")
        self._download(camera, sources.jpeg, jpeg_target)
        with self._state_lock:
            self._latest_jpeg = jpeg_target
            self._latest_capture_profile = PROFILE_RAW
        self._download(camera, sources.nef, nef_target)
        return CapturePair(jpeg=jpeg_target, nef=nef_target)

    @staticmethod
    def _delete_scan(camera: object, sources: RemoteCapturePair) -> None:
        with ExitStack() as deletions:
            deletions.callback(camera.file_delete, sources.nef.folder, sources.nef.name)
            deletions.callback(camera.file_delete, sources.jpeg.folder, sources.jpeg.name)

    @staticmethod
    def _target(path: str | Path, extensions: set[str]) -> Path:
        target = Path(path)
        if target.suffix.casefold() not in extensions:
            raise ValueError(f"Expected destination extension: {', '.join(sorted(extensions))}")
        if not target.parent.is_dir():
            raise FileNotFoundError(target.parent)
        if target.exists():
            raise FileExistsError(target)
        return target

    @staticmethod
    def _extension(path: object) -> str:
        return Path(path.name).suffix.casefold()

    @staticmethod
    def _stem(path: object) -> str:
        return Path(path.name).stem.casefold()

    def _status(self) -> dict[str, object]:
        return {
            "control_taken": self._control_taken,
            "connected": self._camera is not None,
            "model": self._model,
            "image_quality": self._image_quality,
            "configured_profile": self._configured_profile,
            "latest_capture_profile": self._latest_capture_profile,
            "iso": self._iso,
            "iso_choices": self._iso_choices,
            "shutter": self._shutter,
            "shutter_choices": self._shutter_choices,
            "latest_jpeg_path": None if self._latest_jpeg is None else str(self._latest_jpeg),
        }
