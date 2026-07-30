from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Callable

import numpy as np  # type: ignore

from ...progress import ProgressCallback


def build_openexr_helper(*, source_path: str | Path, output_path: str | Path) -> Path:
    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    compiler = shutil.which("c++")
    pkg_config = shutil.which("pkg-config")
    if compiler is None:
        raise RuntimeError("OpenEXR helper build requires a C++ compiler named c++")
    if pkg_config is None:
        raise RuntimeError("OpenEXR helper build requires pkg-config")
    if not source.is_file():
        raise ValueError(f"OpenEXR helper source does not exist: {source}")
    if not output.parent.is_dir():
        raise ValueError(f"OpenEXR helper output directory does not exist: {output.parent}")
    if output.exists():
        raise ValueError(f"OpenEXR helper output already exists: {output}")

    package = subprocess.run(
        [pkg_config, "--cflags", "--libs", "OpenEXR"],
        capture_output=True,
        text=True,
    )
    if package.returncode != 0:
        raise RuntimeError(f"pkg-config could not resolve the OpenEXR development package: {package.stderr.strip()}")
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-O2",
            "-DNDEBUG",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wno-unused-parameter",
            str(source),
            "-o",
            str(output),
            *shlex.split(package.stdout),
        ],
        check=True,
    )
    if not output.is_file() or not os.access(output, os.X_OK):
        raise RuntimeError("OpenEXR helper build succeeded without creating an executable")
    return output


def write_scene_linear_exr(
    *,
    helper_path: str | Path,
    backing_path: str | Path,
    output_path: str | Path,
    shape: tuple[int, int, int],
    dtype: object,
    compression: str = "zip",
    tile_size: int = 256,
    working_space: str = "linear-rec2020",
    color_encoding: str = "scene-linear",
    input_order: str = "rgb",
    progress_cb: ProgressCallback | None = None,
    cancel_cb: Callable[[], None] | None = None,
) -> Path:
    helper = Path(helper_path).expanduser().resolve()
    backing = Path(backing_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    partial = Path(f"{output}.partial")

    if not helper.is_file() or not os.access(helper, os.X_OK):
        raise RuntimeError(
            f"OpenEXR helper is not executable: {helper}. "
            "Build it once with build_openexr_helper() before starting a scan."
        )
    if np.dtype(dtype) != np.dtype(np.float32):
        raise ValueError("OpenEXR backing dtype must be float32")
    if (
        not isinstance(shape, tuple)
        or len(shape) != 3
        or shape[2] != 3
        or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in shape)
    ):
        raise ValueError("OpenEXR backing shape must be (height, width, 3)")
    if not backing.is_file():
        raise ValueError(f"OpenEXR backing file does not exist: {backing}")
    expected_size = shape[0] * shape[1] * shape[2] * np.dtype(np.float32).itemsize
    if backing.stat().st_size != expected_size:
        raise ValueError("OpenEXR backing file size does not match its float32 shape")
    if output.suffix.lower() != ".exr":
        raise ValueError("OpenEXR output path must use the .exr extension")
    if not output.parent.is_dir():
        raise ValueError(f"OpenEXR output directory does not exist: {output.parent}")
    if output.exists():
        raise ValueError(f"OpenEXR output already exists: {output}")
    if partial.exists():
        raise ValueError(f"OpenEXR partial output already exists: {partial}")
    if compression not in {"zip", "piz"}:
        raise ValueError("OpenEXR compression must be zip or piz")
    if working_space not in {"linear-rec2020", "acescg"}:
        raise ValueError("OpenEXR working space must be linear-rec2020 or acescg")
    if color_encoding not in {"scene-linear", "working-linear"}:
        raise ValueError("OpenEXR color encoding must be scene-linear or working-linear")
    if input_order not in {"rgb", "bgr"}:
        raise ValueError("OpenEXR input order must be rgb or bgr")
    if isinstance(tile_size, bool) or not isinstance(tile_size, int) or not 16 <= tile_size <= 2048:
        raise ValueError("OpenEXR tile size must be an integer from 16 through 2048")

    height, width, _channels = shape
    tile_rows = (height + tile_size - 1) // tile_size
    label = f"Writing {color_encoding} OpenEXR"
    command = [
        str(helper),
        "--input",
        str(backing),
        "--output",
        str(output),
        "--width",
        str(width),
        "--height",
        str(height),
        "--tile-size",
        str(tile_size),
        "--compression",
        compression,
        "--working-space",
        working_space,
        "--color-encoding",
        color_encoding,
        "--input-order",
        input_order,
    ]
    if cancel_cb is not None:
        cancel_cb()
    if progress_cb is not None:
        progress_cb("write-exr", label, 0, tile_rows, "tile rows")

    process = subprocess.Popen(command, stdout=subprocess.PIPE, text=True)
    succeeded = False
    completed = 0
    try:
        if process.stdout is None:
            raise RuntimeError("OpenEXR helper stdout is unavailable")
        for record in process.stdout:
            fields = record.rstrip("\n").split("\t")
            if len(fields) != 3 or fields[0] != "PROGRESS":
                raise RuntimeError(f"Invalid OpenEXR helper progress record: {record.rstrip()}")
            record_completed = int(fields[1])
            record_total = int(fields[2])
            if record_total != tile_rows or record_completed != completed + 1:
                raise RuntimeError(f"Invalid OpenEXR helper progress record: {record.rstrip()}")
            completed = record_completed
            if cancel_cb is not None:
                cancel_cb()
            if progress_cb is not None:
                progress_cb("write-exr", label, completed, tile_rows, "tile rows")

        return_code = process.wait()
        if return_code != 0:
            raise subprocess.CalledProcessError(return_code, command)
        if completed != tile_rows:
            raise RuntimeError(f"OpenEXR helper reported {completed} of {tile_rows} tile rows")
        if cancel_cb is not None:
            cancel_cb()
        if not output.is_file():
            raise RuntimeError("OpenEXR helper succeeded without creating its output")
        succeeded = True
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()
        if not succeeded:
            output.unlink(missing_ok=True)
            partial.unlink(missing_ok=True)
    return output
