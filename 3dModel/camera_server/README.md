# Camera server optical stand

These models support the standalone server launched with `python -m camera_server`. They are separate from the printer-controlled scan mount in `3dModel/printer_scan/`.

- `optical_stand.step`: a 54 mm tube clamp on a wide freestanding rear mount.
- `monolithic_stand_body.step`: an optional version that fuses the stand base, mast, and fixed clamp carrier into one solid.

Printable items 02-04 form the modular stand. Items 05-06 are the monolithic alternative.

The phrase "should be exceed 350 mm" is interpreted as "must not exceed 350 mm in any axis."

## Freestanding optical mount

The clamp is attached radially from behind through the gap between its lower and upper bands. No mast, beam, socket, or floor rail crosses the vertical 54 mm tube path above or below the clamps.

- Overall size: 300 x 288.959 x 288 mm
- Tube bore: 54 mm
- Clamp outside diameter: 62 mm
- Clamp screw axes: 72 mm apart
- Lower clamp: 8 mm high
- Upper clamp: 25 mm high
- Vertical spacing between clamp bases: 65 mm
- Floor rails: 24 mm wide x 16 mm high
- Head beam: 44 mm wide x 50 mm high

The floor base is one continuous print with no leg joints or leg holes. Each front rail first runs sideways to `X=+/-138`, then turns toward the user to `Y=-120`; the rounded rear rails spread to `X=+/-115`, `Y=145`. The forward rails have inner faces at `X=+/-126`, leaving a verified 246 x 155 mm clear floor area from `X=-123..123`, `Y=-105..50` beneath the tube.

The fixed clamp carrier, broad rear beam, mast contact pad, and socket are one solid. Part 4 now presses downward over the rectangular mast through a closed four-sided socket with a 6 mm rear wall, then seats against an integrated top stop. Its 50.6 x 30.6 mm cavity provides 0.30 mm broad-face clearance around the 50 x 30 mm mast. Four rounded side pads create 0.10 mm interference per side, while four front and four rear bearing pads keep the mast aligned without full-face rubbing. A 1 mm chamfer around the mast top provides a gradual lead-in. There is no separate rear bracket, thin center bridge, or head-joint M5 hardware.

## One-piece stand variant

`printable/05_monolithic_stand_body.stl` combines printable items 2, 3, and the fixed carrier from item 4 into one continuous solid. The mast socket is filled and fused through the base, the carrier is fused across the full mast contact face, and the three obsolete M5 assembly holes are removed.

The lower and upper clamp caps remain removable and are supplied together in `printable/06_monolithic_clamp_caps.stl`. They cannot be fused to the body because the 54 mm camera tube must enter through the open front of the clamps.

- Overall body size: 300 x 288.999 x 288 mm
- Body count: one closed solid
- Tube bore and clamp geometry: unchanged from the modular stand
- M5 stand hardware: not required
- Required printer space: 300 x 289 x 288 mm for the body alone, or about 320 x 310 mm with the recommended brim; a 350 x 350 x 350 mm printer is recommended

Print the body upright in its supplied orientation with a 10 mm brim. Use build-plate-only organic support under the fixed carrier, carefully blocked from the tube bore, screw holes, and insert pockets. Use 6-8 walls and 35-45% gyroid or cubic infill for PETG.

The one-piece version removes joints, but its upright mast is built across horizontal layer interfaces. The modular version's horizontally printed mast is directionally stronger and remains the preferred version for a heavy camera or frequent handling.

## Print files

STL does not contain a unit field; these files use millimeter coordinates. Import them as millimeters and keep the supplied orientations.

| File | Quantity | Bounding box | Supports |
| --- | ---: | --- | --- |
| `printable/02_stand_base.stl` | 1 | 300 x 288.999 x 74 mm | None; requires at least 310 x 300 mm usable area; a 350 mm bed leaves brim clearance |
| `printable/03_rear_mast.stl` | 1 | 50 x 250 x 30 mm | None; 5 mm brim recommended |
| `printable/04_optical_head_parts.stl` | 1 | 180 x 128.813 x 80 mm | Build-plate-only organic support under the fixed carrier as needed; 5 mm brim |
| `printable/05_monolithic_stand_body.stl` | 1 | 300 x 288.999 x 288 mm | Build-plate-only organic support under the carrier; 10 mm brim requires about 320 x 310 mm usable area |
| `printable/06_monolithic_clamp_caps.stl` | 1 layout containing 2 caps | 180 x 13.963 x 25 mm | None |

Recommended PETG starting profile for a 0.4 mm nozzle:

- 0.20 mm layers
- 5-6 walls
- 6 top and bottom layers
- 30-40% gyroid or cubic infill
- 5 mm brim on the mast and head layout

Block support from all bolt holes, heat-set insert pockets, the tube bore, and the complete head socket. The supplied head orientation bridges the 30.6 mm socket cavity; use tuned bridge flow and cooling, then remove any loose strings without sanding the broad cavity walls. Dry-fit the base socket and the head press-fit before final assembly. If the head is too tight, lightly sand only the four rounded side pads in equal increments rather than enlarging the full socket.

## Stand hardware

- Base-to-mast retention: 1 x M5x75 bolt, 1 x M5 nyloc nut, and 2 x M5 washers; omit only if the printed socket is securely retained and load-tested
- 6 x M3x45 screws
- 6 x M3 heat-set inserts sized for 4.3 mm diameter, 8 mm deep pockets
- Optional self-adhesive rubber feet

For the one-piece variant, omit all M5 hardware. It requires only the six M3 screws, six heat-set inserts, and optional feet.

## Stand assembly

1. Insert the mast into the base socket. Install the M5x75 retention bolt unless your printed socket has a secure, load-tested friction fit.
2. Align the head above the mast and press it straight downward over the chamfered end until the internal top stop seats firmly on the mast.
3. Install the six M3 heat-set inserts, place the tube between the jaws, and tighten the clamp screws evenly.

For the one-piece variant, skip steps 1-2. Install the inserts, place the tube against the fixed clamp halves, and attach the two removable caps with the six M3 screws.

Load-test the stand over a protected surface before mounting expensive equipment. Add rubber feet, ballast, or bench anchors if the working surface can slide or the camera is unusually heavy.

## Parametric sources

- `optical_stand.py`: monolithic base, chamfered mast, rear-mounted optical clamp, and press-fit head saddle
- `monolithic_stand_body.py`: fused base, mast, and fixed carrier with the joint holes suppressed
- `monolithic_clamp_caps.py`: two-cap print layout for the one-piece stand
- `print_*.py`: build-plate-oriented STEP layouts
