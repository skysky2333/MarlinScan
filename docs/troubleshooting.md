# Troubleshooting And Recovery

MarlinScan fails operations visibly instead of guessing around camera, motion, file, or metadata errors. Preserve the first error text; later symptoms can be consequences.

## `[-2] Bad parameters`

This is a libgphoto2 response from a camera property write or capture command. A value can still appear selected in the browser while the attempted profile transition was rejected by the camera. MarlinScan only records a profile or shutter after writing it and reading the same value back; a failed request restores properties already changed and preserves the last fully verified profile.

Use this sequence:

1. Wait until the service state is Idle. Do not change ISO/shutter while a still, autofocus, import, or scan owns the camera.
2. Confirm the Nikon is on, awake, directly connected by a data-capable cable, and not showing a camera-side modal or playback screen.
3. Select **Disconnect** in MarlinScan.
4. Close Photos, Image Capture, tethering tools, and any terminal `gphoto2` process.
5. Select **Take control**, then **Connect**.
6. Choose one shutter from the freshly populated list and wait for the Nikon panel to report the verified value.
7. Take a test photo before calibration or scanning.
8. If Analysis works but RAW fails, check that the camera accepts Large / NEF+Fine in its current state and that its storage target/card is available for Quick mode.
9. If the same property still returns `[-2]`, power-cycle the camera, reconnect the USB cable, and repeat from Take control.

Do not fix this by accepting an unverified readback or silently continuing with another quality. That can pair a displayed shutter with a different physical exposure.

## `Select a global shutter speed before capture`

The service has no verified shutter, even if the select element still displays text from an earlier browser state. Refresh status, reconnect the Nikon, select an advertised finite shutter, and wait for the Camera panel to show it. Bulb and undecodable sentinel choices are not valid automated captures.

## Camera Is Detected But Cannot Connect

macOS `ptpcamerad` commonly owns the PTP interface. Use **Take control** immediately before Connect. This terminates only the current user's exact `/usr/libexec/ptpcamerad` process. Camera ownership can return after disconnect or after another photo application opens.

Also verify:

- exactly one Nikon 1 J4 is detected;
- the cable carries data and is not connected through an unreliable hub;
- no other MarlinScan service or `gphoto2` command owns the camera;
- the camera has not slept or opened a USB/playback prompt.

## Scan Is Underexposed Relative To Nikon JPEG

The Nikon JPEG and embedded NEF preview contain Nikon's picture style and tone curve. MarlinScan develops sensor data deterministically with per-tile auto brightness disabled, then applies one global calibration-derived RAW exposure multiplier. A TIFF is not expected to match an arbitrary Nikon picture style exactly.

Check `raw_development.json` for the global exposure transform and compare the scene-linear tile, display TIFF, and stitched preview. If tile TIFF and mosaic match each other, stitching is not the source of darkening. Recalibrate exposure and gray reference under the scan illumination rather than normalizing individual tiles, which would create seams.

## Exposure Did Not Stay Inside A Fixed P99 Range

There is no hard JPEG P99 233-247 requirement. Auto exposure meters robust middle tones, then optimizes RAW headroom. JPEG P99, clipping, RAW P99, saturation, and black occupancy are diagnostics. A high-contrast scene can require clipped highlights and black shadows. MarlinScan accepts the best available exposure and reports a warning when the camera's discrete shutter range cannot hit the optimization target.

## Blur Or Directional Softness

Increase Scan **Settle (ms)** from the 1000 ms default and repeat a small representative area. Also check:

- camera mount and lens barrel rigidity;
- subject flatness and clamps;
- cable forces on the moving assembly;
- printer acceleration and belt tension;
- whether blur direction matches the preceding X, Y, or Z movement;
- focus ROI texture and the fitted focus-map Z range.

Do not use sharpening to diagnose vibration. Inspect a Local RAW tile at the source resolution.

## Cancel Disconnected The Printer

Normal **Cancel** does not intentionally disconnect. It sets a cooperative stop flag, allows the active atomic command to finish, prevents later commands, returns to Idle, and retains the verified position. **Emergency stop** is different: it sends `M112`, faults the session, and requires reconnecting.

If the printer disappeared after Cancel, inspect the first hardware/serial error and USB connection. A serial transport loss is not converted into an ordinary cancellation.

## Restore Saved Position Is Disabled

Restore is available only after connecting to a port with a remembered confirmed position, while coordinates are not already initialized and the printer is not faulted. Once the current session is initialized, restoring another coordinate state is deliberately disabled.

If a previous emergency stop or serial loss invalidated continuity, Home is the correct recovery even when a saved value exists.

## Quick Acquisition Camera Space

Quick mode records each returned remote JPEG and NEF before import. Success, Cancel, and failure all run job-scoped cleanup and restore the prior capture target. It never enumerates or deletes unrelated card files.

If cleanup fails, the scan fails visibly. Use `camera_captures.json` to identify exact job-owned remote paths. Resolve camera connectivity first, then remove only those listed files with a deliberate camera tool. Never bulk-delete the camera card based on filename patterns.

## Incomplete Or Cancelled Scan Folder

Completed local JPEG/NEF pairs remain on disk. Incremental manifests show exactly what succeeded. A partial scan has no right to claim complete mosaic outputs. Keep it for diagnosis or recoverable restitch work, or remove it manually after confirming it contains nothing needed.

Editor cancellation removes its hidden partial revision directory and does not change the original project or published revisions.

## QuPath Asks To Create A Pyramid

Open `mosaic_pyramidal.ome.tif`, not `mosaic_full.tif` and not an older `mosaic_pyramidal.tif`. The current OME file is already tiled and multi-resolution. See [Outputs](outputs.md#mosaic_pyramidalometif) for reader checks.

## Editor Appears Stuck Or Slow

The current editor should never display `Rendering preview` during a slider change or Full/Local switch. Full Image uses a cached 2000 px mosaic; Local RAW performs one tile request. Browser developer tools should show no `/api/editor/preview` POST.

If the canvas is blank, use current desktop Chrome and confirm WebGL2 plus `EXT_color_buffer_float` are available. The renderer fails visibly rather than falling back to a different image pipeline. If a Local RAW request fails, confirm the service was restarted after upgrading and that `/api/editor/tile-preview` exists.

## Service Restart And Live Hardware

Python backend changes require a service restart. Static files can update while an old service is still running, so a newly loaded browser may expect an endpoint that the old in-memory application does not have. Finish or cancel active work deliberately, then restart the service as one version. Do not reload the service during camera import, cleanup, or output publication.
