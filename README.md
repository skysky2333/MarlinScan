# MarlinScan

MarlinScan is a local scanning workstation that uses a Marlin-controlled printer as a motion stage and a USB-tethered Nikon 1 J4 as the image source. It captures overlapping high-resolution tiles from flat or nearly flat subjects and builds a color-consistent mosaic.

The primary application is a Python service with a browser interface. Printer and camera access remain in the local Python process; the browser handles positioning, calibration, scan setup, progress, stopping, and result review.

## Hardware Scope

- A Marlin-compatible printer or motion stage connected over USB serial
- A Nikon 1 J4 connected directly over Micro-USB and controlled through libgphoto2
- A manual lens and fixed illumination
- A neutral gray reference for white-balance calibration

The primary workflow is USB-only. Nikon Wi-Fi is not used. The J4 does not provide live view over its libgphoto2 USB driver, so the browser displays the latest captured JPEG rather than a video stream.

Set aperture and the lens focus ring physically before calibration. MarlinScan never controls either. Its focus calibration changes the camera-to-subject distance with the printer Z axis.

## Safety And Recovery

- Clear the full configured XYZ travel before connecting or moving the printer.
- Keep the camera mount, lens, nozzle, subject, and cables clear throughout homing, jogging, calibration, and scanning.
- Watch the machine while it moves and keep the emergency-stop control accessible.
- Every new printer connection starts with coordinates uninitialized. Run Home (`G28`), deliberately set the current position as the origin (`G92`), or restore a saved position before motion is enabled.
- MarlinScan remembers the last confirmed XYZ position for each serial port. **Restore saved position** sends that position back with `G92`; it does not move or measure the stage. Use it only when the printer has not physically moved since the position was recorded. Home instead if power loss, released motors, manual handling, or any other event may have changed the position.
- Cancel ends normal work cooperatively after the active command finishes and keeps the printer session and initialized coordinates. Use Emergency Stop when motion must be interrupted immediately.
- Emergency Stop sends `M112` and faults the printer session. Reset or power-cycle the printer if its firmware requires it, disconnect and reconnect it in MarlinScan, then initialize coordinates again.
- MarlinScan cannot change the speed of a `G28` homing cycle. Homing speed is set in the printer's Marlin firmware configuration; the browser speed controls apply to commanded moves and jogging only.

Partial captures already written to disk remain available after a stopped or failed scan. Do not treat an incomplete output folder as a completed mosaic.

## Install On macOS

Python `3.10+` is recommended. Install the gphoto2 CLI and its libgphoto2 dependency, plus native libvips and OpenEXR support:

```bash
brew install gphoto2 vips openexr
```

Install the Python dependencies into the `3dprinter` Conda environment from the repository root:

```bash
conda activate 3dprinter
python -m pip install -r requirements.txt
```

The requirements include the Python gphoto2 binding, FastAPI service, RAW developer, image analysis, serial control, and stitching dependencies.

## Launch

From the repository root:

```bash
conda activate 3dprinter
python -m v3se_printer
```

This starts the service on `http://127.0.0.1:8000/` and opens the default browser. It binds only to this Mac. To suppress browser opening or select another local port:

```bash
python -m v3se_printer --no-open
python -m v3se_printer --port 8010
```

The browser defaults test captures, calibration runs, and scans to `./output/` beneath the directory where MarlinScan was launched. You can replace any of those paths before starting an operation.

Without activating Conda first:

```bash
conda run -n 3dprinter python -m v3se_printer
```

## Nikon USB Session

1. Turn on the Nikon J4 and connect it directly to the Mac with a data-capable Micro-USB cable.
2. Close macOS Photos, Image Capture, and any other camera client.
3. In the Nikon panel, select **Take control**. On macOS this terminates `ptpcamerad`, which otherwise owns the camera interface.
4. Select **Connect**. MarlinScan then keeps one persistent gphoto session for capture and configuration.
5. Select the global ISO and shutter from the values reported by the camera, then take a test capture before moving into calibration. Connection sets the initial shutter to `1/6`.

Taking control can interrupt other macOS photo-import applications. Disconnect the Nikon in MarlinScan before using those applications again.

## Positioning And Capture Feedback

Manual moves default to `200 mm/s` on XY and `10 mm/s` on Z. Hold-to-jog defaults to `100 mm/s` on XY and `10 mm/s` on Z. Calibration and scan motion also default to `200 mm/s` on XY and `10 mm/s` on Z. Adjust these values for the mechanics and payload of the specific stage.

Home first runs the printer's normal `G28`, then raises to `Z203` and moves to the bed center at `X110 Y110`. Confirm that this entire path is clear before starting it.

When the Nikon is connected, MarlinScan automatically captures a **Small / JPEG Basic** still after Home, Restore saved position, each completed manual move, and the end of each jog. These preview captures keep the displayed frame current while minimizing transfer time; they are not scan-quality files. The camera's prior capture settings are restored after each preview.

The latest JPEG view includes three draggable regions for exposure, focus, and the gray reference. The inspection tools show a luma waveform, a robust midtone meter, P99 and JPEG-clipping diagnostics for the exposure region, and a `10x` center loupe for checking detail. These are still-image tools because the J4 USB driver does not provide live view.

## Calibration And Scan

1. Connect the printer, then home it, deliberately set the current position as the origin, or safely restore its remembered position.
2. Connect the Nikon and select the global ISO and shutter used by every capture action.
3. Define the rectangular coverage of the finished image and select separate exposure, focus, and gray-card image regions.
4. Use **Single autofocus** at the current position for a flat subject, or **Grid autofocus** when focus height varies across the coverage.
5. Calibrate exposure, autofocus, and gray-card white balance as separate actions, or use **Run all calibration** to perform the combined workflow. Single autofocus applies its measured Z across the coverage; Grid autofocus measures the four quarter-area centers plus the exact center and fits a focus surface from all five peaks.
6. Confirm the measured camera footprint, which defaults to `25 x 17 mm`, and the desired overlap.
7. Start the scan after exposure, a valid flat or grid focus surface, and white balance are all calibrated. Until then, scanning remains disabled.

The ISO and shutter controls in the Nikon panel are the single source of truth for test captures, calibration, and scans. A scan snapshots their live camera values into `scan_params.json`. Auto exposure changes only the global shutter. It first meters a 2-98 percent winsorized JPEG luminance mean toward `128` as a fast coarse seed, then measures the matching NEF and lengthens an underfilled exposure toward 85 percent P99 in the brightest CFA channel. JPEG clipping, RAW P99, RAW saturation, and black pixels remain diagnostics rather than pass/fail requirements because high-contrast scenes cannot always retain both endpoints. If at least one percent of a RAW channel reaches 99.5 percent of sensor white, MarlinScan shortens the shutter by exactly one available step, verifies again, then accepts the best result with a warning if saturation remains. It never changes ISO or selects a shutter longer than one second.

Each focus-grid point probes `1 mm` below and above its starting Z. A clear improvement extends in that direction; an ambiguous response expands in both directions. The fine pass samples at `0.25 mm` and extends past an edge maximum until the global peak is bracketed. Peak confidence is measured against the complete sweep endpoints, so a broad peak or two-point plateau is valid while a genuinely flat curve fails. There is no user-defined focus range; the configured machine Z limits are the safety boundaries. Single autofocus captures a final analysis frame at the selected peak so the displayed image matches the stage position. The browser logs every focus score and each expansion, bracket, prominence, and selection decision.

Grid autofocus samples the four points formed by the 25 and 75 percent positions across the finished-image coverage area's width and height, plus the exact center. One bilinear surface is fitted from all five measured peaks and used only inside the declared coverage. Single autofocus creates a constant-Z surface from its measured point. After either autofocus mode or Run all calibration, bed clicks, absolute XY moves, XY jogging, and scan tiles automatically use the active surface Z. Z-only moves remain available for deliberate adjustment.

Scan bounds describe the finished image's physical coverage, not the camera-center travel. Defaults are `X15-205 mm` and `Y25-205 mm`; each scan minimum or maximum has a **Use current** button for the corresponding live stage coordinate. The outer tile centers are inset from those bounds by half the `25 x 17 mm` camera footprint, so the outer image edges meet the requested coverage. Tile spacing follows the selected overlap.

The Position map previews the separated tile grid from the current scan settings before capture. While scanning it shows the serpentine planned route, completed route, current location, completed count, active phase, and estimated phase time remaining. Status refreshes more frequently while work is active, and successful calibration or scanning opens Results automatically without an acknowledgement step.

Normal acquisition downloads each JPEG first so it can appear promptly, then its matching NEF, while RAW development overlaps the following tile. Optional **Quick acquisition** stores every JPEG+NEF pair on the camera card during stage motion, records the remote-to-tile mapping, and imports all pairs before development and stitching. Quick acquisition shortens the moving phase but tile images do not appear until import; normal acquisition remains the default.

Quick acquisition deletes every JPEG+NEF pair owned by that job and restores the previous camera storage target after success, Stop, or failure. It never enumerates or deletes unrelated card files. A deletion or storage-restore failure remains visible as a failed job so camera-space leaks are not hidden.

White balance comes from the selected neutral gray-card region in one matching JPEG+NEF capture. The reference must retain usable, unsaturated samples in every CFA channel. That pair also establishes one global linear exposure multiplier and records the camera-to-Rec.2020 matrix. All three are reused for every tile; per-tile auto white balance and automatic brightness are not used. The recipe also pins the rawpy and LibRaw versions, and regeneration fails if the installed engine differs. RAW development leaves saturated highlights unreconstructed and performs the working-space conversion in float, so unclipped channels and signed out-of-gamut values remain in the scene-linear master instead of being forced to a 16-bit boundary.

The browser estimates the scan sampling density below Camera footprint from the latest Large analysis or RAW capture. It shows X, Y, and nominal DPI and retains that estimate when a Small preview is captured.

The Measurements panel keeps the current session's exposure readings, focus scores, selected peaks, and white-balance gains, including the capture profile used for each measurement.

## Image Roles And Outputs

Each captured tile has four matching files:

- `.nef`: untouched Nikon RAW archival source
- `.jpg`: fine JPEG used for exposure and focus analysis, alignment, and quick inspection
- `_scene_linear.tif`: internal float32 linear Rec.2020 working tile
- `.tif`: deterministic 16-bit sRGB development of the same linear tile for inspection

MarlinScan uses three named Nikon profiles: `preview` is Small / JPEG Basic, `analysis` is Large / JPEG Fine, and `raw` is Large / NEF+Fine. The J4 does not advertise a remotely configurable color-space setting, so profiles contain only camera-supported controls. The Nikon panel shows both the configured profile and the profile that produced the latest image.

A scan directory also contains `scan_params.json`, `raw_development.json`, the incrementally written local `captures.json` and developed `tiles.json` manifests, a losslessly compressed tiled float32 `mosaic_scene_linear.exr`, a flat 16-bit `mosaic_full.tif`, a tiled multi-resolution 16-bit `mosaic_pyramidal.ome.tif`, an 8-bit `mosaic_thumb_2000.jpg`, and exact alignment transforms in `stitch_meta.json`. JPEGs establish alignment once; the same geometry composites the linear tiles, and both display TIFFs are derived from that shared mosaic. The pyramidal OME-TIFF has the same full-resolution pixels and sRGB profile as the flat TIFF, plus reduced-resolution SubIFD levels and OME metadata for whole-slide viewers such as QuPath. Quick acquisition additionally writes `camera_captures.json` as each pair is stored on the camera.

The NEFs, manifests, calibration state, development recipe, and transforms are the canonical editable project. The OpenEXR mosaic is a demosaiced scene-linear derivative that preserves wide-gamut float values above display white for large-scale editing. The TIFF tiles and mosaics remain display-referred sRGB images: demosaicing, calibrated white balance, sRGB conversion, gamma, and channel clipping are baked into them. ISO is fixed when the sensor is exposed and cannot be changed afterward; retain the untouched NEFs when original sensor data or a different development recipe matters.

## Image Editor

The **Editor** workspace loads completed RAW scan projects independently of the capture controls. It previews either the complete mosaic or one local RAW-derived tile, and provides global basic, advanced, positive-film, and negative-film controls. Negative-only controls stay hidden in positive mode. The editor intentionally has no brushes, masks, selections, dodge, or burn tools.

Custom scan output locations are remembered by the service, so their completed projects remain available in Editor after a restart.

**Preview** renders the current recipe without changing project files. **Apply to all RAW** develops every original NEF with the shared recipe, reuses the scan's saved alignment transforms, and writes a new immutable numbered revision beneath the scan folder. Each revision contains a linear Rec.2020 working EXR, flat and pyramidal 16-bit TIFFs, a JPEG preview, and the exact edit and revision metadata. Original NEFs and earlier revisions are never overwritten.

Gigapixel scans are normally edited through the full-image proxy or a local RAW tile, then rendered tile by tile and composed with the saved geometry. This is also how large aerial and whole-slide systems avoid loading a complete image into interactive memory. The full-resolution NEF-backed project and float EXR are the archival/editable path; DNG is not the canonical output because common RAW editors cannot reliably open BigDNG mosaics of this size.

## Legacy Desktop And UVC Tools

The earlier Tk desktop application remains available explicitly:

```bash
python -m v3se_printer.app
```

It retains the existing UVC preview, printer maintenance, temperature, tuning, EEPROM, mouse-follow, and older scan tools. Those UVC workflows are separate from the Nikon J4 web workflow and do not add USB live view to the J4.

The standalone UVC browser server is also separate:

```bash
python -m camera_server
```

Only one process can own most UVC cameras at a time. On macOS, grant the terminal or Python launcher camera permission when using UVC hardware.

## Helper Tools

Rebuild outputs from an existing compatible scan folder:

```bash
python tools/restitch_scan.py output/scans/scan_YYYYMMDD_HHMMSS
```

Benchmark realtime serial motion:

```bash
python tools/rt_bench.py --port /dev/cu.wchusbserial1120 --dx 0.5 --feed 6000 --count 80
```

## Repository Layout

- `v3se_printer/web/`: local browser service and interface
- `v3se_printer/service.py`: scan workflow orchestration
- `v3se_printer/nikon.py`: Nikon J4/libgphoto2 camera control
- `v3se_printer/printer.py`: bounded Marlin motion control
- `v3se_printer/calibration.py`: exposure and printer-Z focus calibration
- `v3se_printer/raw.py`: fixed-white-balance 16-bit RAW development
- `v3se_printer/scan/`: tile alignment, composition, and output writing
- `v3se_printer/app.py` and `v3se_printer/ui/`: legacy Tk/UVC application
- `camera_server/`: standalone legacy UVC browser server
