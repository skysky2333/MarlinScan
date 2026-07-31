# Architecture

## Process Ownership

One local Python service owns the printer serial connection, Nikon gphoto session, calibration state, operation state, manifests, and output publication. The browser is a state-driven client. It does not talk directly to USB devices or infer that an operation succeeded.

Only one scanner operation owns the shared operation lock. Read-only status and result access continue while appropriate; conflicting capture, motion, preview-tile generation, and editor apply requests fail.

## State Model

Important states include connecting, disconnecting, idle, moving, jogging, calibrating, scanning, editing, stopping, and faulted.

Normal Cancel sets a cooperative cancellation event. Every long phase checks it between atomic work units. Emergency Stop bypasses that contract, sends Marlin `M112`, and faults the printer session.

Errors preserve the first cause, publish it through status, and return recoverable camera/calibration failures to Idle. Printer faults remain Faulted until reconnection.

## Capture Pipeline

Normal acquisition follows this flow:

```text
move XYZ -> settle -> capture JPEG+NEF -> download JPEG -> publish latest image
                                      -> download NEF -> develop scene tile
next move/capture overlaps prior host-side RAW development
```

Quick acquisition changes only the acquisition/storage boundary:

```text
move/settle/capture pairs to camera card
  -> import exact recorded JPEG+NEF pairs
  -> delete job-owned remote pairs and restore capture target
  -> develop and stitch locally
```

Every capture is recorded before later processing. Job-owned camera cleanup runs in the operation cleanup path on success, cancellation, and failure.

## Calibration Contract

Exposure, focus, and white balance are global scan inputs. Auto exposure selects one shutter, focus creates either a constant or bilinear Z surface, and gray calibration creates one fixed sensor-channel balance. The matching calibration JPEG+NEF pair also determines one global RAW display exposure multiplier.

Per-tile automatic brightness and white balance are prohibited because they would turn local content differences into mosaic seams.

## RAW And Stitching

Each NEF is demosaiced into scene-linear camera RGB, transformed through the calibrated camera-to-XYZ/Rec.2020 matrix, and retained as float32. A separate display conversion produces the 16-bit sRGB tile.

Fine JPEGs establish alignment geometry. Scene-linear and display mosaics reuse that exact geometry. Float compositing preserves signed values and values above one. Large mosaics use memory mapping and tiled writers rather than requiring all outputs in RAM.

## Output Publication

Writers create artifacts in the active job/revision area and publish only completed files. Metadata declares exact output names and roles. Results endpoints allowlist known artifact keys and bind editor downloads to the last published revision.

The OME-TIFF writer creates a full-resolution tiled base plus reduced SubIFD levels and OME metadata. BigTIFF addressing and pyramid levels are independent properties; the output has both.

## Editor Preview

The browser editor has one Full and one Local WebGL2 texture. JPEG inputs are decoded without browser color conversion, decoded from sRGB to linear, transformed to Rec.2020, processed with recipe v2, transformed back to sRGB, and displayed. Recipe changes are batched with `requestAnimationFrame`. These proxies intentionally trade RAW headroom and exact pre-composite nonlinear behavior for interactive speed; apply remains the authoritative float32 render.

Temporary white-balance and film-base rectangles read the decoded proxy through a float WebGL framebuffer. They produce global recipe estimates and then disappear.

The server tile endpoint returns only a neutral RAW-derived local tile. There is no full-mosaic edit-preview POST. This prevents interactive edits from acquiring the operation lock and recompositing every tile.

## Editor Apply

An apply operation reads original NEFs, not the JPEG proxy or prior TIFF. It writes a new `revision-NNN` directory, uses saved alignment, and regenerates all authoritative derivatives. Failure or cancellation removes the unpublished partial revision.

Recipe v2 is strict and records material mode, Basic controls, RGB balance, film parameters, master curve, and eight-band HSL arrays. The Python and WebGL implementations share operation order and validated ranges.

## Module Map

| Module | Responsibility |
| --- | --- |
| `v3se_printer/service.py` | ownership, state, orchestration, cancellation, progress |
| `v3se_printer/nikon.py` | J4 configuration, capture, download, remote cleanup |
| `v3se_printer/printer.py` | bounded serial motion and remembered position |
| `v3se_printer/calibration.py` | exposure, focus sweeps/surfaces, white balance |
| `v3se_printer/raw.py` | pinned deterministic scene-linear development |
| `v3se_printer/editor.py` | projects, recipe v2, revisions |
| `v3se_printer/scan/` | alignment, composition, EXR/TIFF/preview writers |
| `v3se_printer/web/server.py` | strict HTTP/WebSocket boundary |
| `v3se_printer/web/static/` | Capture UI and GPU editor |

## Product Requirements

[PRD.md](../PRD.md) is the authoritative high-level product contract. It deliberately excludes endpoint and function implementation details.
