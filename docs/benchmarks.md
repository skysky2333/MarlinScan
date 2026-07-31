# Measured Benchmark

This benchmark documents one real scan so output-size claims remain reproducible and appropriately qualified.

## Job

| Property | Measured value |
| --- | ---: |
| Date | 2026-07-30 |
| Capture positions | 140, 10 x 14 |
| Finished coverage | 190 x 180 mm |
| Camera footprint | 25 x 17 mm |
| Overlap | 25% |
| ISO / shutter | ISO 160 / 1/13 s |
| Focus | five-point fitted grid surface |
| Acquisition | Quick |
| Recorded settle for this job | 250 ms |
| Output dimensions | 38,324 x 36,167 |
| Output pixels | 1,386,076,508 |
| Resolution metadata | 196.767 px/mm, 4,998 DPI |
| Project size | 67 GB |
| Operation start to final JPEG preview | about 53 minutes |

The product default was increased after this job to 1000 ms stabilization because later inspection found some blur. Do not quote the measured 250 ms as a recommended value.

## Pixel-Count Comparison

| Capture | Nominal output | MarlinScan ratio |
| --- | ---: | ---: |
| Measured MarlinScan mosaic | 1,386 MP | 1.0x |
| Sony A7R V single frame | about 60.2 MP | 23.0x |
| Sony A7R V Pixel Shift | 240.8 MP | 5.75x |
| Phase One IQ4 150MP | about 151 MP | 9.16x |

Sources: [Sony A7R V specifications](https://www.sony.com/electronics/support/e-mount-body-ilce-7-series/ilce-7rm5/specifications), [Sony Pixel Shift](https://electronics.sony.com/imaging/interchangeable-lens-cameras/full-frame/p/ilce7rm5-b), and [Phase One IQ system overview](https://www.phaseone.com/wp-content/uploads/2021/12/IQ_Camera_System_Overview_Flyer.pdf).

These ratios compare output pixel counts only. MarlinScan requires a static, nearly flat subject and many exposures over time. It does not inherit a high-end camera's single-shot dynamic range, noise, color accuracy, lens performance, motion tolerance, or calibrated metrology. Effective resolved detail must be measured from the final image; nominal pixels and DPI are sampling metrics.

## Editor Interaction Benchmark

Desktop Chrome, the real completed project, and the local service produced:

| Interaction | Time | Network behavior |
| --- | ---: | --- |
| Load Full Image proxy | 194-264 ms | one display-proxy JPEG |
| Load first Local RAW tile | 890-895 ms | one RAW-derived tile JPEG |
| Return to cached Full Image | 25-29 ms | no request |
| Slider input through two frames | 25-27 ms | no request |

The former server-side full preview took about 85-89 seconds because it opened, edited, aligned, and blended all 140 float tiles for every click. That path is no longer part of the browser contract.

## Output Validation

The measured `mosaic_pyramidal.ome.tif` has a 38,324 x 36,167 uint16 RGB base, 256 x 256 tiles, eight reduced SubIFDs, ICC profile, resolution metadata, and OME XML. It is a genuine pyramid, not merely BigTIFF addressing.

The scene-linear OpenEXR is float32 tiled Rec.2020. Values above display white are retained before sRGB delivery conversion.

## Reproducing A Benchmark

Record:

1. exact hardware, lens, extension, aperture, light, and subject;
2. scan and RAW-development manifests;
3. operation start and final publication timestamps;
4. output dimensions and DPI metadata;
5. project and scratch disk usage;
6. a resolved-detail target, not only pixel dimensions;
7. any cancellation, warning, retry, or manual intervention.

Do not combine results from different recipes or silently exclude failed time from end-to-end duration.
