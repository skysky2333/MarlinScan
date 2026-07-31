# MarlinScan Documentation

MarlinScan turns a Marlin-controlled motion platform and tethered Nikon 1 J4 into a local, calibrated, gigapixel scanning workstation. The browser controls capture and global RAW development; the Python service exclusively owns hardware, files, operation state, and recovery.

## Start Here

1. [Hardware and physical setup](hardware.md)
2. [Install on macOS](install-macos.md)
3. [Five-minute workflow](quick-start.md)
4. [Capture workspace](capture-workspace.md)
5. [Image editor](editor.md)

## Reference

- [Configuration reference](configuration.md): every important default, range, unit, and folder
- [Outputs and large-image workflows](outputs.md): NEF, EXR, TIFF, OME pyramid, QuPath, and archival roles
- [Troubleshooting and recovery](troubleshooting.md): camera ownership, capture errors, cancellation, saved position, Quick cleanup, and incomplete jobs
- [Architecture](architecture.md): process ownership, state machine, capture pipeline, and editor revisions
- [Measured benchmark](benchmarks.md): the 1.386 GP example and honest camera comparisons
- [HTTP API](api.md): endpoint groups, validation, status, progress, and WebSocket jogging

## What The System Produces

The canonical project is not one giant DNG. It is the original NEFs plus calibration, manifests, the pinned RAW-development recipe, and saved alignment transforms. Those sources can regenerate the image without throwing away sensor data.

The full-resolution editable derivative is a tiled float32 scene-linear Rec.2020 OpenEXR. Delivery and inspection use a flat 16-bit sRGB BigTIFF, a genuine tiled multi-resolution OME-TIFF, and a compact JPEG preview. This is the same general strategy used by aerial imagery and whole-slide systems: retain source tiles, use overviews for navigation, and defer full-resolution rendering.

## Measured Example

| Property | Result |
| --- | ---: |
| Tile positions | 140 |
| Mosaic dimensions | 38,324 x 36,167 |
| Output pixels | 1.386 gigapixels |
| Resolution metadata | 4,998 DPI |
| Working project | 67 GB |
| Operation start to final preview | about 53 minutes |

The output has about 23 times the pixels of a 61 MP Sony A7R V frame and 9.2 times the pixels of a 151 MP Phase One IQ4 frame. That comparison describes spatial output only. MarlinScan scans a static plane over time and is not equivalent to either camera in speed, dynamic range, noise, color, optics, or motion capture.

[![MarlinScan hardware in motion](images/scanner-rig.jpg)](media/scanner-in-motion.m4v)

The linked clip is an 8-second 720p SDR derivative of the supplied hardware video. The original HDR recordings remain outside the documented application workflow.

## Safety Boundary

Cancel is cooperative: the active atomic hardware or file operation finishes, then no later step starts. The printer stays connected and its verified position remains available. Emergency Stop sends Marlin `M112`, faults the printer session, and requires reconnection and coordinate initialization. Never use remembered-position restore after motors were released, the machine lost power, or the stage moved physically.
