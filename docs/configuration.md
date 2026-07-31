# Configuration Reference

MarlinScan currently has no broad application configuration file. Runtime hardware choices and job parameters are explicit in the browser, while the local bind address and port are launch options. Operation manifests record the values used for each calibration and scan.

## Launch

| Setting | Default | Valid values |
| --- | --- | --- |
| Bind address | `127.0.0.1` | Localhost only in the packaged launcher |
| Port | `8000` | `1-65535` |
| Open browser | yes, after 0.8 s | `--no-open` disables it |

```bash
python -m v3se_printer --port 8010 --no-open
```

Run from the intended working directory. Relative output paths resolve from that directory.

## Output Folders

| Operation | Browser default |
| --- | --- |
| Test capture | `./output/test-captures` |
| Calibration | `./output/calibration` |
| Scan | `./output/scans` |

Each operation creates a uniquely named child where required. Scan roots used successfully are recorded so Editor can rediscover completed projects after a service restart.

## Machine Envelope And Position

| Setting | Default or range |
| --- | --- |
| X bounds | `0-220 mm` |
| Y bounds | `0-220 mm` |
| Z bounds | `0-250 mm` |
| Initial browser target | X110, Y110, Z203 |
| Post-home target | X110, Y110, Z203 |
| Absolute XY speed | `200 mm/s` |
| Absolute Z speed | `10 mm/s` |
| Jog XY speed | `100 mm/s` |
| Jog Z speed | `10 mm/s` |
| Maximum XY command speed | `300 mm/s` |
| Maximum Z command speed | `50 mm/s` |

The service enforces bounds and focus-surface Z independent of browser validation. Firmware controls `G28` homing speed.

## Serial

| Setting | Default | Browser choices |
| --- | --- | --- |
| Baud | `115200` | `115200`, `250000`, `230400`, `57600` |
| Line ending | CRLF | CRLF or LF |

The selected port is also the identity used for remembered-position storage.

## Nikon

| Setting | Default |
| --- | --- |
| ISO after connection | `160` |
| Shutter after connection | `1/6 s` |
| Longest auto-exposure shutter | `1 s` |
| Preview profile | Small / JPEG Basic |
| Analysis profile | Large / JPEG Fine |
| RAW profile | Large / NEF+Fine |

ISO and shutter choices are populated from values advertised by the attached camera. Unsupported or global-shutter sentinel values are not offered as valid captures. Aperture and lens focus are physical settings.

## Inspection Regions

Exposure, Focus, and Gray each default to the center 60 percent:

```json
{"x": 0.2, "y": 0.2, "width": 0.6, "height": 0.6}
```

Coordinates are normalized to image width and height. Each region must remain fully inside the image.

## Coverage And Scan Planning

| Setting | Default | Range |
| --- | ---: | ---: |
| X minimum | 15 mm | `0-220` |
| X maximum | 205 mm | `0-220` |
| Y minimum | 25 mm | `0-220` |
| Y maximum | 205 mm | `0-220` |
| Camera frame width | 25 mm | positive |
| Camera frame height | 17 mm | positive |
| Overlap | 25% | `0.1-89.9%` |
| Stabilization | 1000 ms | `0-5000 ms` |
| Scan XY speed | 200 mm/s | `>0-300` |
| Scan Z speed | 10 mm/s | `>0-50` |
| Quick acquisition | off | off/on |
| Route | serpentine | fixed on |

Calibration and Scan fields are synchronized views of one coverage rectangle. Minimum must be less than maximum. The stage center path is inset from finished-image edges by half the camera footprint.

At defaults, MarlinScan plans 10 columns by 14 rows, or 140 tiles. Nominal steps are 18.75 x 12.75 mm; redistributing the outer centers to meet the exact requested edges yields approximately 18.333 x 12.538 mm.

## Autofocus

| Setting | Value |
| --- | ---: |
| Coarse Z step | 1 mm |
| Fine Z step | 0.25 mm |
| Minimum peak prominence | 10% above both sweep endpoints |
| Grid positions | 25%/75% Cartesian points plus exact center |
| Single model | constant Z |
| Grid model | bilinear surface fitted from all five observations |

The search expands until a peak is bracketed or a hard Z limit is reached. A flat sweep fails. A two-sample plateau can be valid and selects its midpoint.

## Exposure

| Setting | Value |
| --- | ---: |
| JPEG coarse target | robust middle-tone value 128 |
| JPEG trim | 2nd-98th percentiles |
| Coarse tolerance | 1/3 EV |
| RAW optimization target | brightest CFA-channel P99 at 85% of black-to-white range |
| Meaningful saturation threshold | at least 1% of a channel at 99.5% sensor white |

JPEG P99, JPEG clipping, RAW P99, RAW saturation, and black occupancy are reported diagnostics. None is a universal acceptance requirement. The selected best exposure can retain clipped highlights or black shadows in a high-contrast scene.

## Output Encoding

| Setting | Value |
| --- | --- |
| Working color space | scene-linear Rec.2020 |
| Editable mosaic | tiled float32 OpenEXR, ZIP compression, 256 px tiles |
| Display mosaic | 16-bit sRGB BigTIFF, Deflate |
| Pyramidal mosaic | 16-bit sRGB OME-TIFF, 256 px tiles, reduced SubIFDs |
| Preview | maximum 2000 px, JPEG quality 88 |
| Alignment | JPEG-derived, saved and reused |
| Blend | weighted feather |
| Per-tile auto brightness/WB | disabled |
| Highlight reconstruction | disabled in the canonical recipe |

## Editor Recipe V2

All values are global and finite. Array lengths are strict.

| Control | Default | Range |
| --- | ---: | ---: |
| Material | `positive` | `positive`, `color_negative`, `bw_negative` |
| Exposure | 0 EV | `-8..8` |
| Temperature, tint, contrast, highlights, shadows | 0 | `-1..1` |
| Black point | 0 | `-1..0.95` |
| White point | 1 | `0.01..8`, greater than black |
| Saturation | 1 | `0..3` |
| RGB balance | 1 each | `0.1..4` |
| Film base RGB | 1 each | `0.01..4` |
| Film density | 1 | `0.1..4` |
| Film Dmin | 0 | `-4..4` |
| Film Dmax | 4 | `-4..8`, greater than Dmin |
| Film red/blue ratios | 1 | `0.1..4` |
| Slide fade repair | 0 | `0..1` |
| Slide black RGB | 0 | finite, each below its white |
| Slide white RGB | 1 | finite, each above its black |
| Master curve | `[0,.25,.5,.75,1]` | five values in `0..1` |
| HSL hue | eight zeros | eight values in `-30..30` degrees |
| HSL saturation/lightness | eight zeros | eight values in `-1..1` |

Editor recipe JSON is strict: version must be `2`, every declared field must be present in the saved recipe, and unknown fields fail validation.

## Persisted Evidence

Important job settings are written into `scan_params.json`, `raw_development.json`, `tiles.json`, and `stitch_meta.json`. Do not edit these files in place. Create a new scan or editor revision so the evidence and output remain consistent.
