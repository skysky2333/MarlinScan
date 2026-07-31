# Install On macOS

MarlinScan is developed around the `3dprinter` Conda environment and native Homebrew imaging libraries.

## Native Packages

```bash
brew install gphoto2 vips openexr
```

`gphoto2` supplies libgphoto2 camera support. libvips handles large tiled TIFFs, and OpenEXR supplies the scene-linear master writer dependencies.

Verify:

```bash
gphoto2 --version
vips --version
```

## Conda Environment

Activate the existing environment and install the pinned repository requirements:

```bash
conda activate 3dprinter
python --version
python -m pip install -r requirements.txt
```

The current validated environment uses Python 3.11. Do not install NegPy into this environment: its current project requires Python 3.13, ships a desktop GUI dependency stack, exposes no stable headless API, and its published wheel omits its processing packages.

Verify imports and tests without hardware:

```bash
python -m unittest discover -s tests
```

## Camera Permissions And Ownership

Connect the Nikon directly with a data-capable cable. macOS may start Photos, Image Capture, or `ptpcamerad`. Close photo applications. MarlinScan's **Take control** action terminates the current user's PTP daemon before it initializes the camera.

Do not run a separate `gphoto2` command while MarlinScan is connected; PTP camera interfaces generally have one owner.

## Serial Permission

The browser lists serial ports visible to pyserial. If the printer is missing:

- reconnect its USB cable;
- check System Information for the USB device;
- confirm the expected `/dev/cu.*` entry exists;
- close slicers, terminal consoles, and other serial clients;
- verify the printer's USB serial driver if it requires one.

## Launch

From the repository directory:

```bash
conda activate 3dprinter
python -m v3se_printer
```

Or without activating first:

```bash
conda run -n 3dprinter python -m v3se_printer --no-open
```

The default URL is `http://127.0.0.1:8000/`. It is intentionally local-only.

## Working Directory

Launch from the repository or another deliberate job directory. Browser defaults are relative paths under `./output`, resolved against the service working directory. Starting the service from an unexpected directory changes where these paths point.

## Optional Documentation Browser Test

Playwright is available in the validated Conda environment. Desktop Chrome can exercise the WebGL editor against a completed scan. Hardware-free unit tests remain the default CI boundary; do not automate Home, motion, camera capture, deletion, or a full scan without a person supervising the physical machine.

## Update Procedure

1. Finish or cooperatively cancel active work.
2. Confirm Quick camera cleanup completed.
3. Stop the service.
4. Update code and requirements.
5. Run the complete test suite.
6. Start a fresh service so static files and backend endpoints use the same version.
7. Take a test photo and run a small validation scan before a large job.
