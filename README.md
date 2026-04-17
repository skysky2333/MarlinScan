# v3se

`v3se` is a Tkinter workstation for creating large-format, high-resolution scans with a Marlin-based printer, built around the Ender-3 V3 SE. The core idea is simple: use the printer as an XY motion stage, capture full-resolution camera tiles across the bed, and stitch them into a single large mosaic. It includes a 3d model to securly mount arbitrary scanner head or lens to the printer.

Printer control, camera setup, preview, autofocus, and realtime motion tools are all there to support that scanning workflow.

The app is still run directly from the repo. There is no packaging layer yet.

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
- The `3dModel/scaner_mount.stl` file is a related hardware model for the scanner/camera setup.

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
- `tools/`: CLI helpers
- `3dModel/`: related hardware model files
