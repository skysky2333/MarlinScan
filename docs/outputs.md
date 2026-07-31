# Outputs And Large-Image Workflows

MarlinScan separates archival sources, editable derivatives, delivery images, and navigation previews. No one file serves every purpose.

## Canonical Editable Project

The canonical project is the combination of:

- untouched Nikon `.nef` tiles;
- matching fine JPEGs;
- `captures.json` and `tiles.json` manifests;
- calibration and scan parameters;
- `raw_development.json` with pinned engine versions and global transforms;
- `stitch_meta.json` with exact alignment geometry.

This preserves the greatest future flexibility. Stitching requires demosaicing and geometric warping, so no stitched panorama remains original CFA sensor RAW. ISO is physically set during capture and cannot be changed afterward in NEF, DNG, EXR, or TIFF.

## Per-Tile Files

| File | Role | Editing latitude |
| --- | --- | --- |
| `.nef` | Untouched sensor archive | Highest; original CFA samples and camera metadata |
| `.jpg` | Nikon rendering, analysis, alignment, quick inspection | Lowest; 8-bit camera tone and color baked in |
| `_scene_linear.tif` | Internal float32 linear Rec.2020 working tile | High; demosaiced but values above white and signed gamut retained |
| `.tif` | Deterministic 16-bit sRGB display tile | Moderate; WB, color conversion, gamma, and clipping baked in |

Quick viewers often make a NEF look like the Nikon JPEG because they display the full-size embedded JPEG preview. That is not the sensor data MarlinScan develops.

## Mosaic Files

### `mosaic_scene_linear.exr`

The full-resolution editable derivative is tiled float32 scene-linear Rec.2020 OpenEXR. It retains values above display white and signed wide-gamut values, making it suitable for high-dynamic-range global operations and film inversion. It is demosaiced and stitched, not original sensor RAW.

OpenEXR supports large tiled images and deferred tile access. Use software with genuine tiled OpenEXR support; applications that insist on decoding the whole 1.386 GP frame may require very large memory.

### `mosaic_full.tif`

This is the flat full-resolution 16-bit sRGB BigTIFF. BigTIFF uses 64-bit file offsets so files can exceed 4 GB. It does not by itself provide reduced zoom levels.

Use this artifact for applications that need one conventional full-resolution, display-referred image and can read BigTIFF.

### `mosaic_pyramidal.ome.tif`

This is a real tiled multi-resolution pyramid. The measured output contains a full-resolution 16-bit RGB base, 256 x 256 tiles, eight reduced-resolution SubIFDs, an sRGB ICC profile, resolution metadata, and OME XML.

QuPath should open this exact `.ome.tif` file without generating another pyramid. If QuPath prompts to create one:

1. verify the filename is `mosaic_pyramidal.ome.tif`, not an older `mosaic_pyramidal.tif` or `mosaic_full.tif`;
2. choose the OME-TIFF/OME image server when QuPath offers a reader choice;
3. inspect Image properties and confirm multiple resolutions exist;
4. update QuPath if its current reader does not recognize SubIFD OME pyramids.

A dynamic QuPath pyramid is its own cache and does not prove that MarlinScan's file lacks pyramid levels.

### `mosaic_thumb_2000.jpg`

The JPEG is a maximum-2000-pixel navigation and sharing image. Editor uses it as the instant Full Image proxy. It is never an archival or quantitative master.

## Metadata And Manifests

| File | Contents |
| --- | --- |
| `scan_params.json` | bounds, tile plan, ISO, shutter, focus model, WB, speeds, settle, Quick mode |
| `raw_development.json` | camera matrix, working space, WB, global exposure, engine versions, output transform |
| `captures.json` | incrementally recorded local JPEG/NEF capture pairs |
| `camera_captures.json` | Quick-mode remote camera paths and intended tile mapping |
| `tiles.json` | developed tile roles and coordinates |
| `stitch_meta.json` | source/tile/canvas dimensions, saved transforms, blend, DPI, outputs |

Manifests are fail-fast contracts. An incomplete tile is not written as successful.

## How Very Large Images Are Edited

Gigapixel aerial, map, and whole-slide workflows normally avoid a single giant DNG. They retain source images and transforms, store tiled full-resolution derivatives, build reduced overviews, and render regions on demand.

For MarlinScan:

- navigate and make interactive decisions with the JPEG proxy and one local RAW-derived tile;
- use the EXR or individual scene-linear tiles in software that supports tiled float images;
- apply one versioned recipe tile by tile when a global change must be authoritative;
- reuse alignment instead of stitching again from scratch;
- retain NEFs for a future RAW engine, camera profile, or alternate film model.

## Why DNG Is Not Canonical

The DNG specification can represent demosaiced LinearRaw data and BigTIFF offsets, so a full-resolution BigDNG can be standards-valid. Compatibility is the limiting factor: common Adobe and open-source RAW applications do not reliably edit gigapixel BigDNG mosaics, and Adobe Camera Raw/Lightroom impose image-size limits below the measured 1.386 GP output.

A linear panorama DNG also cannot restore the original Bayer samples after demosaic, warping, and blending. MarlinScan therefore keeps NEFs plus recipes/transforms as the archival source and OpenEXR as the full-resolution editable derivative. A future BigDNG can only be an explicitly experimental export.

## Storage Planning

The measured 140-tile project uses about 67 GB before editor revisions. Each full editor revision can add another EXR, two TIFF mosaics, display tiles, and scratch work during construction. Keep substantially more free space than one final output set; twice the expected project size is a practical minimum, and more is appropriate when preserving revisions.

Quick acquisition reduces USB transfer during stage motion but does not eliminate transfer or processing. Remote files are deleted from the camera after import or cancellation; local completed captures remain for recovery.
