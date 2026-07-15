#!/usr/bin/env python3
"""
gfx_png_convert.py

Converts between the raw .gfx texture format used by this game (as seen in
TextureFinder v2.1 by IceBerg) and standard .png files, and back again.
Supports single-image .gfx files (like colour.gfx) and multi-region texture
atlases that pack several sprites/icons into one file (like computer.gfx and
computer2.gfx) - including atlases where different regions use different
pixel formats.

FILE FORMAT (reverse-engineered and verified byte-for-byte on real files):
----------------------------------------------------------------------------
  Offset  Size    Meaning
  0       4       count: number of image regions packed in this file (>=1)
  4       4       reserved (observed values: 0 or 1; preserved on rebuild)
  8       4       derived value = 8 + count*4 (a redundant header-size
                   marker; recomputed automatically, never needs to be
                   supplied)
  12      (count-1)*4
                   end-of-region byte offsets for regions 0..count-2
                   (region count-1's end is simply the end of the file)
  12+(count-1)*4  4
                   width (uint16) + height (uint16) of region 0
  ...     region 0 pixel data: width*height*2 bytes (see PIXEL FORMATS below)
  ...     for each subsequent region: 4 bytes (uint16 width, uint16 height)
                   followed by that region's pixel data (width*height*2 bytes)

A plain single-image file (like colour.gfx) is just the case count=1: no
offset table, header is 16 bytes, exactly as originally documented.

PIXEL FORMATS
----------------------------------------------------------------------------
Every region is 2 bytes/pixel, little-endian, but WHICH 2-byte format a
region uses is NOT stored anywhere in the file - there is no discoverable
per-region format flag. It has to be determined by looking at the decoded
image. Formats confirmed in use so far:

  argb1555:  bit 15 = alpha (1 bit), bits 14-10 = R (5 bits),
             bits 9-5 = G (5 bits), bits 4-0 = B (5 bits)
             -> the common case: colour.gfx, computer.gfx, most regions of
                computer2.gfx, the intro/map/metal/note/rock/vactube files,
                and the green font set (compcharsetgreen.gfx)

  argb4444:  bits 15-12 = alpha (4 bits), bits 11-8 = R (4 bits),
             bits 7-4 = G (4 bits), bits 3-0 = B (4 bits)
             -> used by the large "photo-like" regions of computer2.gfx,
                and by numbers.gfx (a red LCD-style digit display, 0-9 plus
                two smaller punctuation/separator glyphs)

  a8l8:      bits 15-8 = alpha (8 bits), bits 7-0 = luminance/gray (8 bits),
             R=G=B=luminance
             -> used by the white font set (compcharset.gfx) - a classic
                grayscale-plus-alpha format for anti-aliased text glyphs

Since the format can't be auto-detected reliably (a naive smoothness
heuristic was tried and rejected - it's biased toward whichever format has
coarser quantization, regardless of correctness), this script:
  - defaults every region to argb1555 the first time it converts a file
  - writes the format it used for each region into atlas_info.json
  - if you re-run the conversion and atlas_info.json already exists next to
    the output, it reuses whatever formats are recorded there

So the correction workflow for a NEW atlas file is: convert once, look at
the region PNGs, and for any region whose colors look wrong, edit its
"format" entry in atlas_info.json (valid values: "argb1555", "argb4444"),
then convert that .gfx again to regenerate with the corrected colors.

USAGE
----------------------------------------------------------------------------
  EASIEST: drag and drop
      Drag one or more .gfx or .png files - or a "..._parts" folder produced
      by this script - onto this script (gfx_png_convert.py) in Windows
      Explorer. It auto-detects what to do and leaves the window open
      showing what happened.

      - Drop a single-image .gfx  -> get one .png (+ a small .header sidecar)
      - Drop a multi-region .gfx  -> get a "<name>_parts" folder containing
        one PNG per region (region_00_WxH.png, region_01_WxH.png, ...) plus
        atlas_info.json (which records the format used per region)
      - Drop a .png (with its .header sidecar alongside) -> get a .gfx back
      - Drop a "<name>_parts" folder -> get a rebuilt <name>.gfx, packing
        the region PNGs back together in numeric order using the formats
        recorded in atlas_info.json

  Command line (for scripting / explicit control):
      python3 gfx_png_convert.py to-png input.gfx [output.png]
      python3 gfx_png_convert.py to-gfx input.png output.gfx [--header original.gfx]
      python3 gfx_png_convert.py to-gfx some_folder_parts [output.gfx]

  Editing an atlas: open the individual region_NN_WxH.png files, edit them
  in any image editor, KEEP THE SAME PIXEL DIMENSIONS, save, then drop the
  whole folder back onto this script.
"""

import argparse
import hashlib
import json
import math
import struct
import sys
import traceback
from pathlib import Path

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    Image = None
    PIL_AVAILABLE = False


# ---------------------------------------------------------------------------
# Pixel formats: each is a (decode, encode) pair of functions.
#   decode(v: int) -> (r, g, b, a) each 0-255
#   encode(r, g, b, a) -> int (the raw 16-bit pixel value)
# ---------------------------------------------------------------------------

def _decode_argb1555(v):
    a = (v >> 15) & 0x1
    r = (v >> 10) & 0x1F
    g = (v >> 5) & 0x1F
    b = v & 0x1F
    return (r * 255) // 31, (g * 255) // 31, (b * 255) // 31, 255 if a else 0


def _encode_argb1555(r, g, b, a):
    r5 = (r * 31 + 127) // 255
    g5 = (g * 31 + 127) // 255
    b5 = (b * 31 + 127) // 255
    a1 = 1 if a >= 128 else 0
    return (a1 << 15) | (r5 << 10) | (g5 << 5) | b5


def _decode_argb4444(v):
    a = (v >> 12) & 0xF
    r = (v >> 8) & 0xF
    g = (v >> 4) & 0xF
    b = v & 0xF
    return r * 17, g * 17, b * 17, a * 17


def _encode_argb4444(r, g, b, a):
    r4 = (r * 15 + 127) // 255
    g4 = (g * 15 + 127) // 255
    b4 = (b * 15 + 127) // 255
    a4 = (a * 15 + 127) // 255
    return (a4 << 12) | (r4 << 8) | (g4 << 4) | b4


def _decode_a8l8(v):
    l = v & 0xFF
    a = (v >> 8) & 0xFF
    return l, l, l, a


def _encode_a8l8(r, g, b, a):
    # source is expected to be grayscale (R=G=B) already, since that's what
    # decoding this format always produces; average as a safe fallback in
    # case the PNG was edited with real color in it
    l = (r + g + b) // 3
    return (a << 8) | l


def _decode_l8(v):
    # plain 8-bit grayscale, no alpha channel at all - always fully opaque
    return v, v, v, 255


def _encode_l8(r, g, b, a):
    return (r + g + b) // 3


# --- l8_shadow_NN: a special whole-image format, not a per-pixel lookup ---
#
# Used by note00/01/02.gfx: a torn-paper photo (opaque grayscale) is drop-
# shadowed onto a transparent background. The shadow and the true background
# share overlapping low byte values (background=0, shadow's flat body=1,
# with a handful of anti-aliasing values 2-39 at boundaries) - AND some of
# those same low values (especially 1) also occur as isolated dark pixels
# deep inside the paper (text ink), which must stay fully opaque.
#
# The distinguishing signal is connectivity: flood-filling from the image
# border through all pixels below a threshold reaches the true background +
# shadow (one connected blob touching the border); dark text pixels inside
# the paper are NOT connected to the border (the paper's brighter pixels
# wall them off). So:
#   - pixels reachable from the border with value 0      -> fully transparent
#   - pixels reachable from the border with value >= 1    -> black, at the
#     given shadow opacity (confirmed 50% by comparison against a real
#     in-game screenshot; format name encodes the percentage, e.g.
#     l8_shadow_50 = 50% opaque, l8_shadow_75 = 75% opaque)
#   - every other pixel (the paper itself, including isolated dark text)
#     -> opaque grayscale using the raw value as luminance
#
# NOTE: this makes the format lossy in one narrow sense - the handful of
# anti-aliasing values (2-39) within the connected border blob all collapse
# to the same flat shadow alpha on decode, so a PNG -> .gfx round trip
# reconstructs the dominant regions exactly but may not reproduce the exact
# original byte at those specific antialiasing edge pixels (visually
# imperceptible; confirmed on note00.gfx to affect well under 1% of pixels).

L8_SHADOW_PREFIX = "l8_shadow_"
L8_SHADOW_THRESHOLD = 40  # raw values below this are candidates for background/shadow


def is_l8_shadow_format(fmt: str) -> bool:
    return fmt.startswith(L8_SHADOW_PREFIX)


def _l8_shadow_alpha(fmt: str) -> int:
    pct = int(fmt[len(L8_SHADOW_PREFIX):])
    return round(pct * 255 / 100)


def _decode_l8_shadow_image(pixel_data: bytes, width: int, height: int, fmt: str) -> "Image.Image":
    """Pure-Python flood fill from the image border through low-value pixels
    to find the connected background+shadow region, distinguishing it from
    isolated dark pixels (text ink) inside the paper. No numpy/scipy needed,
    to keep this tool's only dependency Pillow."""
    shadow_alpha = _l8_shadow_alpha(fmt)
    pixels = pixel_data[:width * height]

    is_low = bytearray(1 if pixels[i] < L8_SHADOW_THRESHOLD else 0 for i in range(width * height))
    visited = bytearray(width * height)

    from collections import deque
    queue = deque()

    def maybe_enqueue(idx):
        if is_low[idx] and not visited[idx]:
            visited[idx] = 1
            queue.append(idx)

    for x in range(width):
        maybe_enqueue(x)                              # top row
        maybe_enqueue((height - 1) * width + x)       # bottom row
    for y in range(height):
        maybe_enqueue(y * width)                      # left col
        maybe_enqueue(y * width + (width - 1))        # right col

    while queue:
        idx = queue.popleft()
        y, x = divmod(idx, width)
        if x > 0:
            maybe_enqueue(idx - 1)
        if x < width - 1:
            maybe_enqueue(idx + 1)
        if y > 0:
            maybe_enqueue(idx - width)
        if y < height - 1:
            maybe_enqueue(idx + width)

    img = Image.new("RGBA", (width, height))
    out = bytearray(width * height * 4)
    for i in range(width * height):
        v = pixels[i]
        if visited[i]:
            out[i * 4 + 3] = 0 if v == 0 else shadow_alpha  # black, background/shadow
        else:
            out[i * 4 + 0] = v
            out[i * 4 + 1] = v
            out[i * 4 + 2] = v
            out[i * 4 + 3] = 255
    img.frombytes(bytes(out))
    return img


def _encode_l8_shadow_image(img: "Image.Image") -> bytes:
    img = img.convert("RGBA")
    width, height = img.size
    src = img.tobytes()
    out = bytearray(width * height)
    for i in range(width * height):
        r, g, b, a = src[i * 4], src[i * 4 + 1], src[i * 4 + 2], src[i * 4 + 3]
        if a == 0:
            out[i] = 0
        elif a < 255 and r == 0 and g == 0 and b == 0:
            out[i] = 1  # the flat shadow value
        else:
            out[i] = max(1, (r + g + b) // 3)  # opaque grayscale; never 0 (reserved for transparent)
    return bytes(out)


# Each format entry is (bytes_per_pixel, decode_fn, encode_fn).
# decode_fn(raw_int) -> (r, g, b, a); encode_fn(r, g, b, a) -> raw_int
PIXEL_FORMATS = {
    "argb1555": (2, _decode_argb1555, _encode_argb1555),
    "argb4444": (2, _decode_argb4444, _encode_argb4444),
    "a8l8":     (2, _decode_a8l8, _encode_a8l8),
    "l8":       (1, _decode_l8, _encode_l8),
}

DEFAULT_FORMAT = "argb1555"  # matches every single-image file seen so far,
                              # and the majority of regions in every atlas seen

# Files where the "reserved" header field is 2 use 1-byte-per-pixel regions
# instead of the usual 2. Every such file confirmed so far (note00/01/02/11)
# is a torn-paper photo drop-shadowed onto a transparent background at 50%
# shadow opacity, so l8_shadow_50 is the default guess rather than plain l8.
# This is a safe default even for a hypothetical future reserved=2 file that
# turns out to be a plain opaque image with no transparency need: the
# shadow/background flood fill in l8_shadow_50 only ever touches pixels that
# are BOTH low-value AND connected to the image border, so if an image has
# no dark border at all, l8_shadow_50 decodes identically to plain l8 anyway.
DEFAULT_FORMAT_1BPP = "l8_shadow_50"

# reserved=1 is used for multiple different things across confirmed files -
# every LARGE (>=50 region) reserved=1 atlas seen so far is a font/character
# set needing a8l8 (compcharset, menucharset, startcharset, bigmenucharset:
# 218-224 regions each), while every SMALL reserved=1 file needs something
# else entirely (numbers.gfx/scannernum.gfx: argb4444 at 13/40 regions;
# computer2.gfx: mixed argb1555+argb4444 at 15 regions) - so region count is
# used to pick between the two defaults, rather than reserved alone.
RESERVED1_CHARSET_REGION_THRESHOLD = 50
DEFAULT_FORMAT_RESERVED1_CHARSET = "a8l8"


def default_format_for_reserved(reserved: int, count: int = 1) -> str:
    if reserved == 2:
        return DEFAULT_FORMAT_1BPP
    if reserved == 1 and count >= RESERVED1_CHARSET_REGION_THRESHOLD:
        return DEFAULT_FORMAT_RESERVED1_CHARSET
    return DEFAULT_FORMAT


# Some atlases mix formats PER REGION in a way no general rule can predict
# (e.g. small icons needing argb1555 alongside large photos needing
# argb4444 in the very same file) - once such a file has been fully
# diagnosed by hand, its exact correction is recorded here, keyed by the
# SHA256 of the raw .gfx bytes so it only ever applies to files confirmed
# byte-for-byte identical to the one actually analyzed, never a guess.
KNOWN_FILE_REGION_FORMATS = {
    # computer2.gfx: every region (icons, the green grid overlay, the Mars
    # terrain photo, and the two illustrated covers) is argb4444 - not a
    # mix with the default argb1555 as originally thought. The icon
    # regions (0-6, 10, 11, 13, 14) looked like plausible icon shapes under
    # the wrong format too (argb1555), which is what let the mistake slip
    # through initially; only checking the actual decoded hue (not just
    # shape legibility) caught it.
    "db697f2579c76c86ef20866af325c1d01c6318d9d7c93f686d816d1f89f40f37": {
        i: "argb4444" for i in range(15)
    },
    # numbers.gfx: red LCD-style digit display, all 13 regions argb4444.
    "672fdb02cbaa6524ed6faa05f3fa359a0241396ed524c571561cf177ed5a8b65": {
        i: "argb4444" for i in range(13)
    },
    # scannernum.gfx: grayscale scanner readout digits, all 40 regions argb4444.
    "112c23e1fc0d5f386a80e5feed4128b9aa95b7198f3e577016776ecb21b70e40": {
        i: "argb4444" for i in range(40)
    },
}


def known_region_format_overrides(data: bytes) -> dict:
    """Exact per-region format corrections for a handful of fully-diagnosed
    files with per-region format mixes that no general rule predicts.
    Returns {region_index: format} or {} if this file isn't one of them."""
    digest = hashlib.sha256(data).hexdigest()
    return dict(KNOWN_FILE_REGION_FORMATS.get(digest, {}))


def bytes_per_pixel(fmt: str) -> int:
    if is_l8_shadow_format(fmt):
        return 1
    return PIXEL_FORMATS[fmt][0]


def decode_pixels_to_image(pixel_data: bytes, width: int, height: int, fmt: str) -> "Image.Image":
    """Decode raw pixel data into a Pillow RGBA image using the given format."""
    if is_l8_shadow_format(fmt):
        return _decode_l8_shadow_image(pixel_data, width, height, fmt)

    bpp, decode_fn, _ = PIXEL_FORMATS[fmt]
    expected = width * height * bpp
    if len(pixel_data) < expected:
        raise ValueError(
            f"Pixel data too short: got {len(pixel_data)} bytes, "
            f"expected {expected} for {width}x{height} at {bpp} byte(s)/pixel"
        )

    img = Image.new("RGBA", (width, height))
    out = bytearray(width * height * 4)
    for i in range(width * height):
        if bpp == 1:
            v = pixel_data[i]
        else:
            v = pixel_data[i * bpp] | (pixel_data[i * bpp + 1] << 8)
        r, g, b, a = decode_fn(v)
        out[i * 4 + 0] = r
        out[i * 4 + 1] = g
        out[i * 4 + 2] = b
        out[i * 4 + 3] = a
    img.frombytes(bytes(out))
    return img


def encode_image_to_pixels(img: "Image.Image", fmt: str) -> bytes:
    """Encode a Pillow image into raw pixel data using the given format."""
    if is_l8_shadow_format(fmt):
        return _encode_l8_shadow_image(img)

    bpp, _, encode_fn = PIXEL_FORMATS[fmt]
    img = img.convert("RGBA")
    width, height = img.size
    src = img.tobytes()
    out = bytearray(width * height * bpp)
    for i in range(width * height):
        r = src[i * 4 + 0]
        g = src[i * 4 + 1]
        b = src[i * 4 + 2]
        a = src[i * 4 + 3]
        v = encode_fn(r, g, b, a)
        if bpp == 1:
            out[i] = v & 0xFF
        else:
            out[i * bpp] = v & 0xFF
            out[i * bpp + 1] = (v >> 8) & 0xFF
    return bytes(out)


# ---------------------------------------------------------------------------
# Core container format: parsing and building (format-agnostic - every
# region is 2 bytes/pixel regardless of which of the formats above it uses)
# ---------------------------------------------------------------------------

def parse_gfx(data: bytes):
    """Parse a .gfx file (single image or multi-region atlas).

    Returns (count, reserved, regions) where regions is a list of
    (width, height, pixel_data_start, pixel_data_end) tuples, in file order.
    """
    if len(data) < 12:
        raise ValueError("File too small to be a valid .gfx file")

    count, reserved, _field8 = struct.unpack_from("<3I", data, 0)
    if not (1 <= count <= 10000):
        raise ValueError(
            f"Region count read as {count}, which doesn't look like a valid "
            f".gfx file (expected a small positive number)"
        )

    bpp = bytes_per_pixel(default_format_for_reserved(reserved, count))

    n_offsets = count - 1
    wh0_off = 12 + n_offsets * 4
    if len(data) < wh0_off + 4:
        raise ValueError("File too small for the declared region count")

    offsets = struct.unpack_from(f"<{n_offsets}I", data, 12) if n_offsets else ()
    wh0 = struct.unpack_from("<I", data, wh0_off)[0]
    w0 = wh0 & 0xFFFF
    h0 = (wh0 >> 16) & 0xFFFF
    header_size = wh0_off + 4

    regions = []
    pos = header_size
    regions.append((w0, h0, pos, pos + w0 * h0 * bpp))
    pos += w0 * h0 * bpp

    for _ in offsets:
        if pos + 4 > len(data):
            raise ValueError("Unexpected end of file while reading a region sub-header")
        w, h = struct.unpack_from("<HH", data, pos)
        pos += 4
        regions.append((w, h, pos, pos + w * h * bpp))
        pos += w * h * bpp

    if pos != len(data):
        raise ValueError(
            f"Parsed regions end at byte {pos} but file is {len(data)} bytes; "
            f"this file may use a variant of the format this script doesn't handle yet"
        )

    return count, reserved, regions


def build_gfx(reserved: int, images, formats) -> bytes:
    """Build raw .gfx bytes from a list of PIL images (region 0 first) and
    a parallel list of per-region format names."""
    count = len(images)
    n_offsets = count - 1
    encoded = [encode_image_to_pixels(im, fmt) for im, fmt in zip(images, formats)]
    w0, h0 = images[0].size
    header_size = 12 + n_offsets * 4 + 4

    offsets = []
    if count > 1:
        pos = header_size + len(encoded[0])
        offsets.append(pos)
        for i in range(1, count - 1):
            pos += 4 + len(encoded[i])
            offsets.append(pos)

    field8 = 8 + count * 4
    header = struct.pack("<III", count, reserved, field8)
    if n_offsets:
        header += struct.pack(f"<{n_offsets}I", *offsets)
    header += struct.pack("<HH", w0, h0)

    body = bytearray(encoded[0])
    for i in range(1, count):
        w, h = images[i].size
        body += struct.pack("<HH", w, h)
        body += encoded[i]

    return header + bytes(body)


def _region_suspicion_note(w: int, h: int, region_bytes: bytes):
    """Return a short human-readable reason if a region looks like unused
    padding/filler rather than a real image (implausible dimensions, or
    content that's just one repeated byte value throughout), or None if it
    looks like an ordinary region. Purely informational - doesn't change
    how the region is decoded or round-tripped."""
    reasons = []
    if w >= 10000 or h >= 10000:
        reasons.append(f"implausible dimensions ({w}x{h})")
    if region_bytes and len(set(region_bytes)) == 1:
        reasons.append(f"every byte is the same value ({region_bytes[0]})")
    return "; ".join(reasons) if reasons else None


def _is_padding_region(w: int, h: int, region_bytes: bytes) -> bool:
    """Unambiguous signal that a region is unused filler, not a real image.
    Requires BOTH an implausible size AND every byte being identical -
    either alone is too weak: a legitimate blank glyph (e.g. a "space"
    character in a font atlas) is genuinely uniform but has a normal size,
    while requiring both together only matches things like the 65535x1
    all-zero region seen in note11.gfx."""
    implausible_size = w >= 10000 or h >= 10000
    uniform = bool(region_bytes) and len(set(region_bytes)) == 1
    return implausible_size and uniform


# ---------------------------------------------------------------------------
# High-level conversion
# ---------------------------------------------------------------------------

def convert_to_png(input_path, output_path=None):
    """.gfx -> .png (single image) or a "<name>_parts" folder (atlas)."""
    input_path = Path(input_path)
    data = input_path.read_bytes()
    count, reserved, regions = parse_gfx(data)

    print(f"Detected {count} region(s) in {input_path.name}, reserved={reserved}")
    default_fmt = default_format_for_reserved(reserved, count)

    if count > 1:
        # Figure out which regions are real vs. unused padding *before*
        # deciding whether to present a folder of parts or a single flat
        # file - a multi-region container with only one real image (the
        # rest being padding) should look and feel like a plain single-image
        # file, not an atlas folder.
        padding_flags = [_is_padding_region(w, h, data[s:e]) for (w, h, s, e) in regions]
        real_indices = [i for i, is_pad in enumerate(padding_flags) if not is_pad]

        if len(real_indices) == 1:
            idx = real_indices[0]
            w, h, s, e = regions[idx]

            out = Path(output_path) if output_path else input_path.with_suffix(".png")

            format_sidecar = Path(str(out) + ".format")
            if format_sidecar.exists():
                fmt = format_sidecar.read_text().strip()
                print(f"Using format override from {format_sidecar.name}: {fmt}")
            else:
                fmt = default_fmt

            regions_meta = []
            for i, (rw, rh, rs, re_) in enumerate(regions):
                if i == idx:
                    print(f"  region {i}: {rw}x{rh}  ({re_ - rs} bytes)  format={fmt}")
                    regions_meta.append({"index": i, "width": rw, "height": rh, "format": fmt})
                else:
                    fill = data[rs]
                    print(f"  region {i}: {rw}x{rh}  ({re_ - rs} bytes)  "
                          f"[UNUSED PADDING - every byte is {fill} - no PNG created]")
                    regions_meta.append({"index": i, "width": rw, "height": rh,
                                          "is_padding": True, "padding_byte": fill})

            img = decode_pixels_to_image(data[s:e], w, h, fmt)
            img.save(out)

            sidecar_path = Path(str(out) + ".regions.json")
            sidecar_path.write_text(json.dumps({
                "reserved": reserved, "count": count, "real_region_index": idx,
                "regions": regions_meta,
            }, indent=2))
            format_sidecar.write_text(fmt)

            print(f"Saved PNG: {out}")
            print(f"({count - 1} other region(s) in the original file were unused "
                  f"padding with no real content, so no folder or extra PNGs were "
                  f"created - {sidecar_path.name} remembers them so converting back "
                  f"reproduces the exact original file.)")
            print(f"If the colors look wrong, edit {format_sidecar.name} (valid "
                  f"values: {', '.join(PIXEL_FORMATS)}, or l8_shadow_NN e.g. "
                  f"l8_shadow_50) and convert this .gfx again to regenerate.")
            return [out]

    if count == 1:
        w, h, s, e = regions[0]

        out = Path(output_path) if output_path else input_path.with_suffix(".png")

        # Allow a previously-written format override sidecar to stick across
        # re-conversions, same idea as atlas_info.json for multi-region files.
        format_sidecar = Path(str(out) + ".format")
        if format_sidecar.exists():
            fmt = format_sidecar.read_text().strip()
            print(f"Using format override from {format_sidecar.name}: {fmt}")
        else:
            fmt = default_fmt

        print(f"  region 0: {w}x{h}  ({e - s} bytes)  format={fmt}")
        img = decode_pixels_to_image(data[s:e], w, h, fmt)
        img.save(out)

        header_bytes = data[:s]
        header_path = Path(str(out) + ".header")
        header_path.write_bytes(header_bytes)
        format_sidecar.write_text(fmt)

        print(f"Saved PNG: {out}")
        print(f"Saved header sidecar: {header_path} (auto-detected next time "
              f"you convert this PNG back to .gfx)")
        print(f"If the colors look wrong, edit {format_sidecar.name} (valid "
              f"values: {', '.join(PIXEL_FORMATS)}, or l8_shadow_NN e.g. "
              f"l8_shadow_50 for a drop shadow baked into a transparent "
              f"1-byte-per-pixel image) and convert this .gfx again to "
              f"regenerate with corrected colors.")
        return [out]

    else:
        stem = input_path.stem
        out_dir = input_path.with_name(stem + "_parts")
        out_dir.mkdir(exist_ok=True)

        # Seed with any known per-region corrections for this EXACT file
        # (see KNOWN_FILE_REGION_FORMATS), then let an existing atlas_info.json
        # override further, so hand-corrections survive re-running the
        # conversion and previously-diagnosed files work right straight away.
        info_path = out_dir / "atlas_info.json"
        prior_formats = known_region_format_overrides(data)
        if prior_formats:
            print(f"Using known format correction(s) for {len(prior_formats)} "
                  f"region(s) in this specific file (diagnosed previously).")
        if info_path.exists():
            try:
                prior = json.loads(info_path.read_text())
                for r in prior.get("regions", []):
                    if "format" in r:
                        prior_formats[r["index"]] = r["format"]
                print(f"Found existing atlas_info.json - reusing its per-region "
                      f"format choices (edit that file to correct any region, "
                      f"then run this conversion again).")
            except Exception:
                pass

        # First pass: classify every region (padding vs real) and decode the
        # real ones, before deciding how to lay them out on disk.
        region_info = []
        decoded = {}  # index -> PIL Image, only for real (non-padding) regions
        for i, (w, h, s, e) in enumerate(regions):
            fmt = prior_formats.get(i, default_fmt)
            region_bytes = data[s:e]

            if _is_padding_region(w, h, region_bytes):
                fill = region_bytes[0]
                region_info.append({
                    "index": i, "width": w, "height": h,
                    "is_padding": True, "padding_byte": fill,
                })
                continue

            decoded[i] = decode_pixels_to_image(region_bytes, w, h, fmt)
            region_info.append({"index": i, "width": w, "height": h, "format": fmt})

        real_regions = [r for r in region_info if not r.get("is_padding")]
        widths = {r["width"] for r in real_regions}
        heights = {r["height"] for r in real_regions}
        all_real = len(real_regions) == len(region_info)

        # Same width, varying height (e.g. a tall image split into chunks by
        # a texture-height limit) -> vertical-stack combined image, in
        # addition to keeping the individual files. Heights must actually
        # vary - same-width AND same-height regions (e.g. a uniform 32x32
        # icon set like objects.gfx) is not a split image, it's an
        # icon/glyph collection and should fall through to grid-packing.
        do_vstack = (len(real_regions) > 1 and all_real and len(widths) == 1
                     and len(heights) > 1)
        # Lots of small, variously-sized regions and no common width -> looks
        # like a character set or icon/digit collection (one glyph/icon per
        # region) -> pack into one grid sheet INSTEAD of individual files,
        # since editing many tiny separate PNGs is impractical. Cell size
        # uses the max width/height actually present so every glyph fits,
        # even if sizes aren't perfectly uniform. The size cap keeps this
        # from firing on atlases that mix small icons with large photos
        # (e.g. computer2.gfx has a 256px-wide region) where a uniform grid
        # would waste huge amounts of space.
        do_grid = (not do_vstack and all_real and len(real_regions) >= 10
                   and max((r["width"] for r in real_regions), default=0) <= 128
                   and max((r["height"] for r in real_regions), default=0) <= 128)

        combined_path = None
        grid_meta = None

        if do_grid:
            cell_h = max(r["height"] for r in real_regions)
            cell_w = max(r["width"] for r in real_regions)
            cols = math.ceil(math.sqrt(len(real_regions)))
            rows = math.ceil(len(real_regions) / cols)
            sheet = Image.new("RGBA", (cols * cell_w, rows * cell_h), (0, 0, 0, 0))
            for pos, r in enumerate(real_regions):
                row, col = divmod(pos, cols)
                sheet.paste(decoded[r["index"]], (col * cell_w, row * cell_h))
            sheet_path = out_dir / f"charset_grid_{cols}x{rows}cells_{cell_w}x{cell_h}.png"
            sheet.save(sheet_path)
            combined_path = sheet_path
            grid_meta = {"cols": cols, "rows": rows, "cell_width": cell_w, "cell_height": cell_h}

            print(f"  {len(real_regions)} small, variously-sized regions with no "
                  f"common width - looks like a character set, so packed into a "
                  f"single {cols}x{rows}-cell grid sheet ({cell_w}x{cell_h} cells) "
                  f"instead of individual files: {sheet_path.name}")
            for r in region_info:
                if r.get("is_padding"):
                    print(f"  region {r['index']}: {r['width']}x{r['height']}  "
                          f"[UNUSED PADDING - byte={r['padding_byte']} - reconstructed "
                          f"automatically, not part of the sheet]")
                else:
                    print(f"  region {r['index']}: {r['width']}x{r['height']}  "
                          f"format={r['format']}")

        else:
            for r in region_info:
                i, w, h = r["index"], r["width"], r["height"]
                if r.get("is_padding"):
                    print(f"  region {i}: {w}x{h}  [UNUSED PADDING - every byte is "
                          f"{r['padding_byte']} - no PNG created, reconstructed "
                          f"automatically on rebuild]")
                    continue
                suspicious = _region_suspicion_note(w, h, data[regions[i][2]:regions[i][3]])
                print(f"  region {i}: {w}x{h}  format={r['format']}"
                      + (f"  [SUSPICIOUS: {suspicious}]" if suspicious else ""))
                out = out_dir / f"region_{i:02d}_{w}x{h}.png"
                decoded[i].save(out)

            if do_vstack:
                total_w = real_regions[0]["width"]
                total_h = sum(r["height"] for r in real_regions)
                combined = Image.new("RGBA", (total_w, total_h))
                y = 0
                for r in real_regions:
                    combined.paste(decoded[r["index"]], (0, y))
                    y += r["height"]
                combined_path = out_dir / f"combined_{total_w}x{total_h}.png"
                combined.save(combined_path)
                print(f"  All regions share width {total_w} - also saved a stitched "
                      f"version: {combined_path.name}")

        if any(r.get("is_padding") for r in region_info):
            print("\nNote: region(s) marked UNUSED PADDING above contain no real "
                  "image data (every byte is the same value) - seen in at least one "
                  "real file (leftover empty space in a localized build). No PNG is "
                  "created for these; atlas_info.json remembers enough to reproduce "
                  "them exactly when converting back to .gfx.")

        info = {
            "reserved": reserved,
            "count": count,
            "source_file": input_path.name,
            "regions": region_info,
            "layout": "grid" if do_grid else ("vstack" if do_vstack else None),
            "combined_image": combined_path.name if combined_path else None,
            "grid": grid_meta,
        }
        info_path.write_text(json.dumps(info, indent=2))

        if do_grid:
            print(f"\nSaved 1 grid sheet PNG ({len(real_regions)} glyphs packed in) "
                  f"to: {out_dir}")
        else:
            n_padding = sum(1 for r in region_info if r.get("is_padding"))
            if n_padding:
                print(f"\nSaved {count - n_padding} region PNG(s) to: {out_dir} "
                      f"({n_padding} region(s) were unused padding, no PNG needed)")
            else:
                print(f"\nSaved {count} region PNGs to: {out_dir}")
        print(f"Saved atlas_info.json there too.")
        print(f"If any region's colors look wrong, edit its \"format\" field in "
              f"atlas_info.json (valid values: {', '.join(PIXEL_FORMATS)}, or "
              f"l8_shadow_NN e.g. l8_shadow_50 for a 50%-opacity drop shadow "
              f"baked into a transparent 1-byte-per-pixel image) and "
              f"convert this .gfx again to regenerate with corrected colors.")
        if do_vstack:
            print(f"You can edit EITHER the individual region_NN files OR "
                  f"{combined_path.name} (whichever is more convenient) - "
                  f"when converting back, whichever one you edited most "
                  f"recently will be used.")
        if do_grid:
            print(f"Edit {combined_path.name} directly (keep every glyph within its "
                  f"own {grid_meta['cell_width']}x{grid_meta['cell_height']} cell, "
                  f"top-left aligned) - there are no individual files to edit instead.")
        print(f"To convert back to .gfx, drag the WHOLE '{out_dir.name}' "
              f"folder onto this script.")
        return [r["index"] for r in region_info]


def convert_to_gfx(input_path, output_path=None, header_path=None):
    """.png (single) or a "<name>_parts" folder (atlas) -> .gfx"""
    p = Path(input_path)

    if p.is_dir():
        info_path = p / "atlas_info.json"
        if not info_path.exists():
            raise ValueError(
                f"No atlas_info.json found inside {p} - this doesn't look like "
                f"a folder produced by this script's to-png step."
            )
        info = json.loads(info_path.read_text())
        reserved = info.get("reserved", 0)
        region_list = info.get("regions", [])
        region_meta = {r["index"]: r for r in region_list}

        if not region_list:
            raise ValueError(f"atlas_info.json in {p} lists no regions")

        # only used to compare mtimes against a possible combined/grid image;
        # padding regions have no file and are handled separately
        region_files = sorted(p.glob("region_*.png"), key=lambda f: int(f.stem.split("_")[1]))

        combined_name = info.get("combined_image")
        combined_path = (p / combined_name) if combined_name else None

        images = []
        formats = []
        layout = info.get("layout")

        if layout == "grid":
            grid = info.get("grid") or {}
            cols = grid.get("cols")
            cell_w, cell_h = grid.get("cell_width"), grid.get("cell_height")
            sheet_path = combined_path
            if not (sheet_path and sheet_path.exists() and cols and cell_w and cell_h):
                raise ValueError(
                    f"atlas_info.json says layout=grid but the sheet image or grid "
                    f"metadata is missing/incomplete in {p}"
                )
            sheet = Image.open(sheet_path).convert("RGBA")
            print(f"Rebuilding {len(region_list)} region(s) from grid sheet "
                  f"{sheet_path.name} ({cols} cols, {cell_w}x{cell_h} cells):")

            real_sorted = [r for r in sorted(region_list, key=lambda r: r["index"])
                           if not r.get("is_padding")]
            pos_by_index = {r["index"]: pos for pos, r in enumerate(real_sorted)}

            for r in sorted(region_list, key=lambda r: r["index"]):
                idx, w, h = r["index"], r["width"], r["height"]
                if r.get("is_padding"):
                    fill = r.get("padding_byte", 0)
                    img = Image.new("RGBA", (w, h), (fill, fill, fill, 255))
                    fmt = "l8"
                    print(f"  region {idx}: {w}x{h}  [reconstructed unused padding, "
                          f"byte={fill}]")
                else:
                    pos = pos_by_index[idx]
                    row, col = divmod(pos, cols)
                    x0, y0 = col * cell_w, row * cell_h
                    img = sheet.crop((x0, y0, x0 + w, y0 + h))
                    fmt = r.get("format", DEFAULT_FORMAT)
                    print(f"  region {idx}: cropped {w}x{h} from cell ({col},{row})  "
                          f"format={fmt}")
                images.append(img)
                formats.append(fmt)

        elif combined_path and combined_path.exists() and (
            combined_path.stat().st_mtime
            > max((f.stat().st_mtime for f in region_files), default=0)
        ):
            print(f"Using {combined_path.name} (edited more recently than the "
                  f"individual region files) - splitting it back into regions.")
            combined_img = Image.open(combined_path).convert("RGBA")
            cw, ch = combined_img.size
            expected_w = region_list[0]["width"] if region_list else cw
            expected_h_total = sum(r["height"] for r in region_list)
            if cw != expected_w:
                print(f"Warning: {combined_path.name} width is {cw}, expected "
                      f"{expected_w}. Proceeding anyway, but the .gfx dimensions "
                      f"will follow the original per-region widths.")
            if ch != expected_h_total:
                print(f"Warning: {combined_path.name} height is {ch}, expected "
                      f"{expected_h_total} (sum of original region heights). "
                      f"Regions will be re-sliced proportionally, which may "
                      f"shift content if the combined image was resized.")

            y = 0
            for r in sorted(region_list, key=lambda r: r["index"]):
                h = r["height"]
                w = r["width"]
                # scale the slice boundaries if the combined image's total
                # height changed (e.g. user resized it) so we still cut the
                # right NUMBER of regions, proportionally
                y0 = round(y * ch / expected_h_total) if expected_h_total else y
                y1 = round((y + h) * ch / expected_h_total) if expected_h_total else y + h
                crop = combined_img.crop((0, y0, cw, y1))
                if crop.size != (w, h):
                    crop = crop.resize((w, h))
                images.append(crop)
                formats.append(r.get("format", DEFAULT_FORMAT))
                print(f"  region {r['index']}: sliced {w}x{h}  format={formats[-1]}")
                y += h
        else:
            print(f"Rebuilding {len(region_list)} region(s) for {p.name}:")
            for r in sorted(region_list, key=lambda r: r["index"]):
                idx, w, h = r["index"], r["width"], r["height"]
                if r.get("is_padding"):
                    fill = r.get("padding_byte", 0)
                    img = Image.new("RGBA", (w, h), (fill, fill, fill, 255))
                    fmt = "l8"
                    print(f"  region {idx}: {w}x{h}  [reconstructed unused padding, "
                          f"byte={fill} - no PNG file needed]")
                else:
                    f = p / f"region_{idx:02d}_{w}x{h}.png"
                    if not f.exists():
                        raise ValueError(
                            f"Expected region file {f.name} not found in {p} - "
                            f"did a file get renamed, moved, or deleted?"
                        )
                    img = Image.open(f)
                    fmt = r.get("format", DEFAULT_FORMAT)
                    print(f"  {f.name}: {img.size[0]}x{img.size[1]}  format={fmt}")
                images.append(img)
                formats.append(fmt)

        if output_path:
            out = Path(output_path)
        else:
            name = p.name
            base = name[:-len("_parts")] if name.endswith("_parts") else name + "_rebuilt"
            out = p.with_name(base).with_suffix(".gfx")

        data = build_gfx(reserved, images, formats)
        out.write_bytes(data)
        print(f"Wrote {out} ({len(data)} bytes, {len(images)} region(s))")
        return out

    else:
        # A single-real-region file that came from a multi-region container
        # (the rest being unused padding) records that in a .regions.json
        # sidecar - if present, rebuild the FULL original structure (real
        # image + reconstructed padding), not just a plain single-region file.
        regions_sidecar = Path(str(p) + ".regions.json")
        if regions_sidecar.exists():
            info = json.loads(regions_sidecar.read_text())
            reserved = info.get("reserved", 0)
            region_list = info.get("regions", [])
            real_idx = info.get("real_region_index", 0)

            img = Image.open(p)

            format_sidecar = Path(str(p) + ".format")
            if format_sidecar.exists():
                fmt = format_sidecar.read_text().strip()
                print(f"Using format override from {format_sidecar.name}: {fmt}")
            else:
                fmt = next((r["format"] for r in region_list if r["index"] == real_idx),
                           default_format_for_reserved(reserved, len(region_list)))

            images, formats = [], []
            for r in sorted(region_list, key=lambda r: r["index"]):
                if r["index"] == real_idx:
                    images.append(img)
                    formats.append(fmt)
                    print(f"  region {r['index']}: {img.size[0]}x{img.size[1]}  format={fmt}")
                else:
                    fill = r.get("padding_byte", 0)
                    pad_img = Image.new("RGBA", (r["width"], r["height"]), (fill, fill, fill, 255))
                    images.append(pad_img)
                    formats.append("l8")
                    print(f"  region {r['index']}: {r['width']}x{r['height']}  "
                          f"[reconstructed unused padding, byte={fill}]")

            data = build_gfx(reserved, images, formats)
            out = Path(output_path) if output_path else p.with_suffix(".gfx")
            out.write_bytes(data)
            print(f"Wrote {out} ({len(data)} bytes, {len(images)} region(s), "
                  f"{len(images) - 1} of which were reconstructed padding)")
            return out

        img = Image.open(p)
        width, height = img.size

        reserved = 0
        if header_path:
            header = Path(header_path).read_bytes()
            if len(header) < 8:
                sys.exit(f"--header file is too small ({len(header)} bytes)")
            reserved = struct.unpack_from("<I", header, 4)[0]
        else:
            auto_sidecar = Path(str(p) + ".header")
            if auto_sidecar.exists():
                header = auto_sidecar.read_bytes()
                if len(header) >= 8:
                    reserved = struct.unpack_from("<I", header, 4)[0]
                print(f"Using auto-detected header sidecar: {auto_sidecar}")
            else:
                print("No original header available; assuming reserved=0 "
                      "(matches every single-image file seen so far).")

        format_sidecar = Path(str(p) + ".format")
        if format_sidecar.exists():
            fmt = format_sidecar.read_text().strip()
            print(f"Using format override from {format_sidecar.name}: {fmt}")
        else:
            fmt = default_format_for_reserved(reserved)

        data = build_gfx(reserved, [img], [fmt])
        out = Path(output_path) if output_path else p.with_suffix(".gfx")
        out.write_bytes(data)
        print(f"Wrote {out} ({len(data)} bytes: {width}x{height} single-region "
              f"image, format={fmt})")
        return out


def cmd_to_png(args):
    convert_to_png(args.input, args.output)


def cmd_to_gfx(args):
    convert_to_gfx(args.input, args.output, args.header)


def run_drag_and_drop(paths):
    """Handle files/folders dragged directly onto the script."""
    print("=" * 60)
    print(" gfx <-> png converter - drag & drop mode")
    print("=" * 60)

    any_errors = False
    for raw_path in paths:
        p = Path(raw_path)
        print(f"\nProcessing: {p.name}")
        try:
            if not p.exists():
                print(f"  ERROR: not found: {p}")
                any_errors = True
                continue

            if p.is_dir():
                convert_to_gfx(p)
                continue

            ext = p.suffix.lower()
            if ext == ".png":
                convert_to_gfx(p)
            elif ext == ".gfx":
                convert_to_png(p)
            else:
                try:
                    Image.open(p).verify()
                    convert_to_gfx(p)
                except Exception:
                    convert_to_png(p)
        except Exception as e:
            print(f"  ERROR processing {p.name}: {e}")
            any_errors = True

    print("\n" + "=" * 60)
    if any_errors:
        print(" Finished with errors - see above.")
    else:
        print(" Done! Converted file(s) saved next to the originals.")
    print("=" * 60)


def main():
    if not PIL_AVAILABLE:
        print("=" * 60)
        print(" ERROR: missing required library 'Pillow'")
        print("=" * 60)
        print()
        print("This script needs the Pillow image library, which isn't installed.")
        print("Open Command Prompt and run:")
        print()
        print("    pip install pillow")
        print()
        print("If that command isn't recognized, Python itself may not be")
        print("installed / on PATH. Install it from https://www.python.org/downloads/")
        print("(check 'Add python.exe to PATH' during setup), then try again.")
        return

    if len(sys.argv) >= 2 and sys.argv[1] not in ("to-png", "to-gfx", "-h", "--help"):
        run_drag_and_drop(sys.argv[1:])
        return

    if len(sys.argv) == 1:
        print(__doc__)
        return

    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_to_png = sub.add_parser("to-png", help="Convert a .gfx file to .png (or a folder of PNGs)")
    p_to_png.add_argument("input", help="Input .gfx file")
    p_to_png.add_argument("output", nargs="?", help="Output .png file (single-image files only)")
    p_to_png.set_defaults(func=cmd_to_png)

    p_to_gfx = sub.add_parser("to-gfx", help="Convert a .png (or a region-folder) back to .gfx")
    p_to_gfx.add_argument("input", help="Input .png file or '<name>_parts' folder")
    p_to_gfx.add_argument("output", nargs="?", help="Output .gfx file")
    p_to_gfx.add_argument("--header", help="Original .gfx (or .header sidecar) to copy "
                                            "the 'reserved' field from, for single-image files")
    p_to_gfx.set_defaults(func=cmd_to_gfx)

    args = parser.parse_args()
    args.func(args)


class _Tee:
    """Writes to multiple streams at once (console + log file)."""
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
            except Exception:
                pass

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass


if __name__ == "__main__":
    log_file = None
    try:
        log_path = Path(__file__).resolve().parent / "conversion_log.txt"
        log_file = open(log_path, "a", encoding="utf-8", buffering=1)
        log_file.write("\n" + "=" * 60 + "\n")
        sys.stdout = _Tee(sys.stdout, log_file)
        sys.stderr = _Tee(sys.stderr, log_file)
    except Exception:
        pass

    try:
        main()
    except SystemExit:
        pass
    except Exception:
        print("\nUNEXPECTED ERROR:")
        traceback.print_exc()
        if log_file is not None:
            print("\n(A full log was also saved to conversion_log.txt next to this script)")

    if log_file is not None:
        try:
            log_file.close()
        except Exception:
            pass

    try:
        input("\nPress Enter to close this window...")
    except Exception:
        pass
