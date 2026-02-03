# v3se (Ender-3 V3 SE serial control)

Small Tkinter app for sending G-code over serial to a Marlin-based printer (tested with Ender-3 V3 SE).

## Setup

- Python 3.10+ recommended
- Install dependency:
  - `python -m pip install pyserial`

## Run

- `python -m v3se_printer`

## Code layout

- `v3se_printer/app.py`: main Tkinter app + serial worker integration
- `v3se_printer/ui/`: tab builders and realtime control logic (split out of the old monolithic GUI file)
- `v3se_printer/gui.py`: backwards-compatible wrapper (re-exports `main` / `PrinterGUI`)

## Safety

- This tool can move the printer immediately. Keep the bed clear and the nozzle at a safe Z height.
- Be ready to hit **EMERGENCY STOP (M112)** in the UI or power off the printer.

## Bed Realtime (mouse tracking)

Open the **Bed Realtime** tab to stream small XY moves that “chase” your mouse position.

How it works:

- The app sends many tiny relative moves (`G91` + repeated `G0 X… Y… F…`).
- Marlin queues motion internally, so this is best-effort and not hard real-time. If you queue moves faster than the printer can execute them, it will lag.

Controls:

- **Start / Stop**: starts/stops streaming. While running, the UI switches the printer to relative mode (G91) and restores your previous mode on stop.
- **Hold left mouse to move**: when enabled (default), the printer only moves while you hold the left mouse button on the canvas.
- **Home X/Y on Start (G28 X Y)**: optional. Note: some firmwares may raise Z slightly during homing even when only X/Y are requested.
- **Tick (Hz)**: update rate. Typical starting range: 20–60 Hz.
- **Max step/tick (mm)**: maximum distance per tick.
- **Deadband (mm)**: don’t move when already close to target (reduces jitter).
- **Buffer (ms)**: small lookahead queue (reduces choppiness). Lower = more responsive, higher = smoother but more input lag.
- **Sync each tick (M400)**: waits for moves to finish each tick. This reduces “queued lag” but can feel steppy and limits max tick rate.
- **Motion Boost (optional)**: temporarily applies `M201`/`M204`/`M205 J` while running (helps short-segment motion), then restores values on stop.

Tuning tip:

- Effective XY speed is approximately `min(SpeedXY, tick_hz * step_mm)`. For example: `40 Hz * 1.0 mm ≈ 40 mm/s`.
- If it’s choppy with tiny steps, increase **Max step/tick** and/or **Tick (Hz)**, or enable **Motion Boost**.

## Realtime Keyboard (Move tab)

The **Move** tab also has **Realtime Keyboard** controls that stream short relative moves while keys are held:

- Arrow keys = X/Y
- Shift = Z+ ; Control = Z-

This uses the same idea as Bed Realtime: tune **Tick (Hz)** and **Buffer (ms)** for responsiveness vs smoothness.

The startup **Homing / Coordinate Setup** dialog also includes a **Manual Positioning (Keyboard Jog)** section that uses the same controls.

## Benchmarking (optional)

If you want to measure how fast your firmware acknowledges small moves:

- `conda run -n 3dprinter python tools/rt_bench.py --port /dev/cu.wchusbserial1120 --dx 0.5 --feed 6000 --count 80`
