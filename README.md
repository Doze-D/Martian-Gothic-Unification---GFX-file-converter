# Martian-Gothic-Unification---GFX-file-converter
Python .PY script tool to convert GFX-files of Martian Gothic: Unification into PNG and back to GFX.

## Container format

```
Offset  Size              Meaning
0       4                 count: number of image regions in this file (>=1)
4       4                 reserved (0, 1, or 2 - see below)
8       4                 derived = 8 + count*4 (redundant, ignored on read)
12      (count-1)*4       end-of-region byte offsets for regions 0..count-2
12+...  4                 width(u16) + height(u16) of region 0
...     region 0 pixel data: width*height*bpp bytes
...     for each subsequent region: 4-byte (u16 width, u16 height) sub-header
        + that region's pixel data
```

`count=1` is just the simple single-image case (16-byte header, no offset
table). Region pixel format is **not stored in the file** - has to be
diagnosed by decoding and visually/numerically checking.

**`reserved` field controls bytes-per-pixel**, not a directly meaningful
value otherwise:
- `reserved=2` → **1 byte/pixel**. Default guess: `l8_shadow_50` (see below).
- `reserved=0` or `1` → **2 bytes/pixel**. Default guess: `argb1555`, except
  `reserved=1` with ≥50 regions defaults to `a8l8` (font sets).

## Pixel formats implemented

All alpha-first ("ARGB" not "RGBA" bit order) where applicable.

| name | bpp | layout | used by |
|---|---|---|---|
| `argb1555` | 2 | A1 R5 G5 B5 | most files (the default) |
| `argb4444` | 2 | A4 R4 G4 B4 | computer2.gfx (all regions), numbers.gfx, scannernum.gfx |
| `a8l8` | 2 | A8 + L8 (grayscale), R=G=B=L | compcharset/menucharset/startcharset/bigmenucharset |
| `l8` | 1 | plain 8-bit grayscale, always opaque | generic reserved=2 fallback |
| `l8_shadow_NN` | 1 | special: flood-fills from image border through low-value pixels to find background+shadow vs. isolated dark "text ink"; shadow gets NN% opacity black, background alpha=0, everything else opaque grayscale. Pure Python (no numpy/scipy) | note00/01/02/11.gfx, all confirmed 50% |

## Known-file registry (`KNOWN_FILE_REGION_FORMATS`)

Some atlases mix formats per-region in ways no general rule predicts.
Diagnosed files are hardcoded by SHA256 so the correction applies
automatically on a **fresh** conversion, not just when a manually-edited
`atlas_info.json` happens to be lying around:
- `computer2.gfx` — all 15 regions `argb4444` (not a mix; originally
  mis-diagnosed as only 4 regions needing it — the icon regions looked
  plausible under the wrong format too, only checking actual hue caught it)
- `numbers.gfx` — all 13 regions `argb4444`
- `scannernum.gfx` — all 40 regions `argb4444`

## Container/tool features

- **Atlases**: multi-region files extract to a `<name>_parts/` folder +
  `atlas_info.json` (records reserved, count, per-region format).
- **Padding regions**: some files (note11.gfx) have bogus regions that are
  100% one repeated byte value with implausible dimensions (leftover junk
  from a localized build) - detected automatically, no PNG created, silently
  reconstructed on rebuild. If a file has exactly one *real* region plus
  padding, it's presented as a flat `<name>.png` (not a folder) with a
  `.regions.json` sidecar - matches the plain single-image workflow.
- **Vertical-stack combined images**: same-width, varying-height regions
  (e.g. intro2.gfx, a tall image split in two) get a stitched
  `combined_WxH.png` *in addition to* individual region files. Editing
  either the combined image or an individual file works - whichever was
  saved most recently wins on rebuild (mtime comparison).
- **Grid-sheet packing**: ≥10 same-ish-sized (all ≤128px) regions get
  packed into ONE `charset_grid_ROWSxCOLScells_WxH.png` sheet **instead
  of** individual files (no per-glyph files at all). Cell size = max
  width/height present; each glyph top-left aligned. Editing the sheet and
  converting back correctly re-crops each region.
  - `do_vstack` requires same width **and varying height** (a genuinely
    split tall image, e.g. intro2.gfx). Same-width-AND-same-height regions
    (e.g. objects.gfx's 132 uniform 32x32 icons) no longer get misclassified
    as vstack - they fall through to grid-packing instead, which is what
    they actually are. Previously the width-only check let vstack claim
    these before grid-packing (`not do_vstack`) got a chance to run.
- **Format override sidecars**: `<file>.png.format` (single-image) or the
  `"format"` field per region in `atlas_info.json` (atlases) - edit and
  re-convert to fix a wrong color guess. These persist across re-runs.
- **Drag-and-drop**: drop `.gfx`/`.png`/a `_parts` folder directly onto the
  script; auto-detects direction. Logs everything to `conversion_log.txt`
  next to the script and always pauses before closing, so failures are
  visible even via double-click.

## Confirmed working files (33)

colour, computer, computer2, intro1, intro2, intro3, intro4, map, map2,
metal, note, rock, vactube, compcharsetgreen, compcharset, led, menucharset,
normalcharset, numbers, objects, pointer, scannernum, startcharset,
startcharset2, subtitlecharset, note00, note01, note02, note11,
bigmenucharset — all byte-perfect round trip (note00/01/02/11 have a
handful of antialiasing-edge bytes that collapse during the shadow format's
lossy region classification — expected, visually imperceptible, confirmed
harmless).

`objects.gfx` (132 uniform 32x32 regions) now grid-packs as a
12x11-cell sheet instead of dumping 132 individual PNGs, confirmed
byte-perfect round trip after the vstack/grid fix above.

## Known non-issues (don't re-investigate these)

- **pointer.gfx magenta pixels** (regions 4,5,6,7,8,11): one specific raw
  value (`0xe419`) decodes to magenta `(205,0,205)` instead of matching its
  clean sibling gray `(205,205,205)` (`0xe739`) - green channel zeroed in
  the *original game file*, not a decode bug. User confirmed: leave as-is,
  it's an unused/leftover file anyway.
- **Any "transparency looks broken" report**: check the actual shipped
  PNG's alpha bytes directly first (`Image.open(...).getpixel(...)`) before
  assuming a bug - this has repeatedly turned out to be stale/cached
  downloads on the user's end, not actual data problems.


Round-trip test pattern:
```python
a = open('original.gfx','rb').read()
b = open('rebuilt.gfx','rb').read()
assert a == b  # or diff and confirm only expected bytes differ
```
