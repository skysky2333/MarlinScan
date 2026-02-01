# v3se (Ender-3 V3 SE serial control)

Small Tkinter app for sending G-code over serial to a Marlin-based printer (tested with Ender-3 V3 SE).

## Setup

- Python 3.10+ recommended
- Install dependency:
  - `python -m pip install pyserial`

## Run

- `python -m v3se_printer`

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
- **Tick (Hz)**: update rate. Typical starting range: 20–60 Hz.
- **Max step (mm)**: maximum distance per tick.
- **Deadband (mm)**: don’t move when already close to target (reduces jitter).
- **Sync each tick (M400)**: waits for moves to finish each tick. This reduces “queued lag” but can feel steppy and limits max tick rate.

Tuning tip:

- Approx commanded XY speed is `tick_hz * step_mm`. For example: `40 Hz * 1.0 mm ≈ 40 mm/s`.

