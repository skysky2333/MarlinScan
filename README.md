# MarlinScan

`MarlinScan` is a scanning workstation for creating large-format, high-resolution scans with a Marlin-based printer, built around the Ender-3 V3 SE. The core idea is simple: use the printer as an XY motion stage, capture full-resolution camera tiles across the bed, and stitch them into a single large mosaic. The repo also includes separate hardware models for printer-controlled scanning and the standalone camera server.

Printer control, camera setup, preview, autofocus, and realtime motion tools are all there to support that scanning workflow.

The app is still run directly from the repo. There is no packaging layer yet. The Python package name remains `v3se_printer` for now, so the launch command is still `python -m v3se_printer`.

## Safety

- This app can move the printer immediately.
- Homing, realtime jogging, autofocus, and scanning all cause motion.
- Keep the bed clear, keep the nozzle at a safe Z height, and watch the machine while it is moving.
- Be ready to use **EMERGENCY STOP (M112)** in the UI or cut power if needed.

## Primary Focus

- Large-format scan capture
  - Full-resolution TIFF tile capture across a configurable XY area
  - Serpentine scan order for efficient bed coverage
  - Multi-shot capture modes: `none`, `best`, `nlmeans`
  - Output folders with saved scan parameters and tile metadata
- Focus control for scan quality
  - Live preview with sharpness readout and history plot
  - Printer-driven Z autofocus
  - Optional focus-mesh calibration
  - Optional autofocus at each tile during the scan
- High-resolution stitched outputs
  - Automatic stitching into a large mosaic TIFF
  - JPEG preview generation for quick inspection
  - Offline restitching from previously captured tiles

## Supporting Capabilities

- Printer connection and control
  - USB serial connect/disconnect
  - Port + baud auto-detection against Marlin using `M115`
  - Background status polling with `M105` / `M114`
  - Firmware config import from `M503`
  - Raw G-code console
- Motion workflows
  - Quick actions for info, homing, leveling, mesh on/off, reset, and EEPROM commands
  - Relative jog, absolute go-to, relative move, and extrude/retract
  - Bed view for point-and-click absolute targeting
  - Realtime keyboard jog
  - Realtime mouse-follow XY control on the bed canvas
- Camera configuration
  - UVC device scan/connect
  - Resolution, FPS, FourCC, rotation, crop, and best-effort UVC control tuning
- Printer tuning
  - Hotend and bed setpoints
  - Fan control
  - Feed override (`M220`), flow override (`M221`), acceleration override (`M204`)
- Helper tools
  - Re-stitch an existing scan folder from saved tiles
  - Benchmark serial `ok` timing for realtime jog tuning

## Hardware Assumptions

- The defaults are tuned around an Ender-3 V3 SE running Marlin-compatible G-code.
- The work area defaults assume a roughly `220 x 220 x 250 mm` printer.
- Other Marlin printers may work, but you should expect to retune bounds, speeds, autofocus ranges, and scan settings.
- The original printer-controlled scan mount is in `3dModel/printer_scan/`.
- The standalone camera-server stand is in `camera_server/cad/printable/`.

## Dependencies

Python `3.10+` is recommended.

Base app:

```bash
python -m pip install pyserial
```

Camera preview, autofocus, and scan capture:

```bash
python -m pip install opencv-python numpy
```

Optional preview rendering fallback:

```bash
python -m pip install pillow
```

Stitched TIFF output and the restitch CLI:

```bash
python -m pip install pyvips
```

Notes:

- `pyvips` also needs a working native `libvips` install on your system.
- Scan tile capture can still run without `pyvips`, but stitched outputs and `tools/restitch_scan.py` will fail.
- Camera scanning and preview are optional; the printer control side only needs `pyserial`.

## Running The App

```bash
python -m v3se_printer
```

## Standalone Camera Stream

The `camera_server` package serves the camera without starting the printer UI. The browser scans the available camera indices, lets you pick one, and opens a full-window preview. The default Detail mode requests `3840x2160` and paces the browser stream at `15 FPS`; Smooth mode provides a `1920x1080` option at the same paced rate. Both modes request `MJPG` capture and accept the resolution the camera actually provides.

Run it from the requested Conda environment:

```bash
conda activate 3dprinter
python -m camera_server
```

Or without activating the environment:

```bash
conda run -n 3dprinter python -m camera_server
```

Open `http://127.0.0.1:8000/`. The default bind is local to this Mac. To expose the viewer on the workstation's LAN addresses, opt in explicitly:

```bash
python -m camera_server --host 0.0.0.0
```

Available endpoints:

- `/`: full-window live viewer
- `/cameras.json`: cached camera discovery results
- `POST /cameras/scan`: rescan camera indices
- `POST /camera`: select a camera and preview profile
- `DELETE /camera`: stop the active preview
- `POST /settings`: set software red, green, and blue gains
- `POST /white-balance`: calibrate or toggle automatic software white balance from a normalized gray region
- `/stream.mjpg`: MJPEG stream at the active profile resolution
- `/snapshot.jpg`: current JPEG at the active profile resolution
- `/status.json`: requested and actual capture details
- `/healthz`: `200` while a fresh camera frame is available

For automatic correction, place a gray or white neutral reference in view, enable **Auto WB**, then drag over that reference when the picker opens. **Pick gray** also works with Auto WB off for a one-time calibration; leaving Auto WB on resamples the same region continuously. Dark, clipped, or stale selections are rejected without changing the current gains. The R/G/B sliders provide a manual override and turn Auto WB off when adjusted.

Both automatic and manual correction use software gains because macOS camera backends often ignore UVC white-balance controls. The server also requests that device-level auto white balance be disabled so it does not fight the software correction when the backend honors that control. Initial manual gains can be set with `--red-gain`, `--green-gain`, and `--blue-gain`.

Only one process can own most UVC cameras at a time, so close the MarlinScan desktop app before starting the standalone server. On macOS, allow the terminal or Python launcher under **System Settings > Privacy & Security > Camera**. Use `python -m camera_server --help` to change the initial profile, scan range, requested capture FPS, FourCC, port, JPEG quality, or color gains.

## Typical Workflow

1. Connect the camera if you need preview, autofocus, or scanning.
   Use `Scan`, then `Connect`, then `Setup` / `Preview` / `Auto Focus (Z)` as needed.
2. Connect the printer.
   Use `Refresh` or `Auto-detect (find port/baud)`, then connect over serial.
3. Complete the startup homing / coordinate setup dialog.
   The app supports both automatic homing (`G28`) and manual zeroing (`G92`).
4. Use the notebook tabs for normal control.
   `Quick`, `Move`, `Bed`, `Bed Realtime`, `Scan`, `Temps/Fan`, `Tuning`, and `Level/EEPROM`.
5. For scans, stop realtime modes first, confirm camera + printer are connected, choose area/step/output settings, and start the scan.

## Typical Scanning Workflow

1. Mount and align the camera for the bed-scanning setup.
2. Connect the camera, open `Setup`, and dial in resolution, crop, focus, and exposure behavior.
3. Connect the printer and complete startup homing / coordinate setup.
4. Use preview and `Auto Focus (Z)` to find a usable capture configuration.
5. In the `Scan` tab, define the XY area, step size, autofocus behavior, multi-shot mode, and output folder.
6. Run the scan to save full-resolution tiles and, if stitching is enabled, build the final mosaic outputs.

## Scan Output Layout

Each scan is written under `scans/scan_YYYYMMDD_HHMMSS/` by default.

The design target is a scan that is much larger than a single camera frame: many full-resolution tiles on disk, plus one stitched high-resolution output when reconstruction succeeds.

Typical contents:

- `scan_params.json`: saved scan settings
- `tiles.json`: tile index with row/column and XY positions
- `tile_r###_c###_x..._y....tif`: captured full-resolution tiles
- `mosaic_full.tif`: stitched mosaic if stitching succeeds
- `mosaic_thumb_2000.jpg`: preview JPEG if preview generation succeeds
- `stitch_meta.json`: stitch metadata
- `stitch_error.txt`: stitch failure details when stitching fails

If stitching fails, the tiles are still kept and can be restitched later.

## Helper Tools

Realtime serial benchmark:

```bash
python tools/rt_bench.py --port /dev/cu.wchusbserial1120 --dx 0.5 --feed 6000 --count 80
```

Restitch an existing scan folder:

```bash
python tools/restitch_scan.py scans/scan_YYYYMMDD_HHMMSS
```

`tools/restitch_scan.py` exposes many tuning flags for blend mode, refinement, DPI metadata, TIFF layout, and preview generation if you need to rebuild outputs without re-running the scan.

## Repo Layout

- `v3se_printer/app.py`: main Tkinter application and integration point
- `v3se_printer/ui/`: notebook tabs for motion, scan, tuning, maintenance, and realtime controls
- `v3se_printer/serial_worker.py`: queued serial I/O worker with immediate-path emergency stop support
- `v3se_printer/uvc.py`: UVC camera config, probing, transforms, and sharpness helpers
- `v3se_printer/scan/`: scan execution, tile I/O, stitching, and output writing
- `camera_server/`: standalone browser camera server and its dedicated CAD models
- `tools/`: CLI helpers
- `3dModel/printer_scan/`: original STEP and STL mount for printer-controlled scanning
