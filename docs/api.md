# HTTP API

The local FastAPI service exposes interactive documentation at `/docs` and its schema at `/openapi.json`. It binds to `127.0.0.1` in the packaged launcher and has no authentication boundary for network deployment.

## Principles

- JSON request models are strict and reject unknown fields.
- Numeric trust boundaries require finite values and enforce physical/control ranges.
- Hardware/state conflicts return HTTP 409.
- invalid requests and recipes return HTTP 422;
- missing files/projects return HTTP 404;
- camera, printer, operating-system, and writer failures return HTTP 503;
- result downloads use allowlisted artifact names rather than client filesystem paths.

## Status

`GET /api/status` is the browser's source of truth. Important fields include:

- `state`, `message`, and `error`;
- `printer` and `camera` status;
- `calibration`, `focus_grid`, and `quick_calibration`;
- `measurements`;
- `scan_progress` geometry;
- one shared `step_progress` object;
- latest scan/editor result descriptors.

Step progress contains a machine step name, user-facing label, completed count, optional total, unit, and optional `eta_seconds`. An omitted total means the phase is adaptive or not yet finite.

## Printer Endpoints

The printer group covers port discovery, connect/disconnect, Home, origin, remembered-position restore, bounded absolute movement, cooperative stop, and emergency stop. Motion requests accept explicit XY and Z speeds within service limits.

Realtime jogging uses `/ws/jog`. Each message contains a normalized direction vector and XY/Z speeds. XY and Z are not combined in one jog vector. The browser renews held motion; release, blur, page hide, or an expired lease stops the command.

## Camera Endpoints

The camera group covers macOS ownership, persistent connection, ISO, global shutter, and test capture. Settings are selected from camera-advertised choices and verified by readback.

## Calibration Endpoints

Separate endpoints start auto exposure, single autofocus, grid autofocus, gray white balance, or the combined calibration plan. Requests contain normalized ROIs, shared physical coverage, folders, and relevant speeds.

Starting an operation returns the current status immediately. The browser polls status for phase updates and completion.

## Scan Endpoint

`POST /api/scan/start` accepts coverage, footprint, overlap, folder, speed, stabilization delay, and Quick acquisition. The server constructs and validates the plan and rejects it unless camera, printer, exposure, focus, and white-balance prerequisites are satisfied.

## Editor Endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /api/editor/projects` | discover completed RAW scan projects |
| `POST /api/editor/project` | load strict project details, tile labels, normalized aligned bounds, and preview URL |
| `GET /api/editor/original-preview` | return the saved 2000 px mosaic JPEG |
| `GET /api/editor/tile-preview` | return one neutral RAW-derived tile JPEG by project and index |
| `POST /api/editor/apply` | start a full recipe-v2 revision |
| `GET /api/editor/results/{artifact}` | download an allowlisted last-revision artifact |

There is intentionally no full edited-preview endpoint. Interactive processing occurs in the browser; full precision occurs in apply.

## File Results

Scan and editor result endpoints expose declared artifacts only. A query path cannot redirect them to an arbitrary local file. Original per-tile files remain inside the scan directory and are not individually exposed as unrestricted download paths by this API.

## Hardware Automation Warning

The API being local and documented does not make unattended physical motion safe. Client code must still respect visible state, machine clearance, camera ownership, and human supervision. Do not issue Home, move, scan, remote deletion, or Emergency Stop as a documentation or health-check request.
