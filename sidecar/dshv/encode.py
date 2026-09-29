"""Pure-Python image encoders (zero third-party dependencies).

Environment fact (M0, Win11 24H2): the system gdiplus.dll is a slimmed build
whose Python-FFI entry points (GdipCreateBitmapFromScanline/HBITMAP/Stream)
return GdiplusUnsupportedCode, so the sidecar encodes itself:

  - PNG: zlib (stdlib) + manual chunks;
  - JPEG: baseline (JFIF) encoder — forward float DCT, standard quantization
    tables scaled by `quality`, standard Annex-K Huffman tables, 4:4:4.

Inputs are 24bpp BGR top-down rows (GDI DIB order); outputs are complete
file bytes. Both encoders are verified against independent decoders
(System.Drawing / read_image) during M0 acceptance.
"""

import struct
import zlib

__all__ = ["bgr_to_rgb", "encode_png", "encode_jpeg", "save_image"]


# ---------------------------------------------------------------------------
# color conversion
# ---------------------------------------------------------------------------


def bgr_to_rgb(buf: bytes, w: int, h: int) -> bytes:
    """BGR (GDI DIB order) top-down -> RGB top-down."""
    n = w * h * 3
    if n == 0:
        return b""
    b = buf[0::3]
    g = buf[1::3]
    r = buf[2::3]
    out = bytearray(n)
    out[0::3] = bytes(r)
    out[1::3] = bytes(g)
    out[2::3] = bytes(b)
    return bytes(out)


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    out = struct.pack(">I", len(data)) + tag + data
    out += struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return out


def encode_png(rgb: bytes, w: int, h: int) -> bytes:
    """Encode RGB8 top-down rows as an 8-bit RGB (color type 2) PNG file."""
    stride = w * 3
    raw = bytearray()
    append = raw.extend
    for y in range(h):
        append(b"\x00")
        append(rgb[y * stride : (y + 1) * stride])
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + _png_chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------------------
# JPEG (baseline, JFIF)
# ---------------------------------------------------------------------------

STD_LUM_QUANT = [
    16, 11, 10, 16, 25, 25, 25, 51,
    12, 12, 14, 19, 26, 25, 51, 56,
    14, 16, 24, 40, 51, 61, 56, 56,
    14, 17, 24, 47, 79, 62, 57, 51,
    14, 17, 24, 47, 79, 62, 57, 51,
    14, 17, 24, 47, 79, 62, 57, 51,
    14, 17, 24, 47, 79, 62, 57, 51,
    14, 17, 24, 47, 79, 62, 57, 51,
]
STD_CHR_QUANT = [
    17, 18, 24, 47, 99, 99, 99, 99,
    18, 18, 24, 47, 99, 99, 99, 99,
    24, 24, 55, 99, 99, 99, 99, 99,
    47, 47, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
]
# Canonical 8x8 JPEG zigzag order (natural_order): raster index visited
# at each zigzag position. Generated from the standard anti-diagonal rule;
# verified to be a permutation of 0..63 with first row 0,1,8,16,9,2,3,10.
ZIGZAG = [
    0, 1, 8, 16, 9, 2, 3, 10,
    17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34,
    27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36,
    29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46,
    53, 60, 61, 54, 47, 55, 62, 63,
]

# JPEG standard Annex-K Huffman tables. Machine-extracted from
# libjpeg-turbo jstdhuff.c (canonical IJG). bits is a 17-slot list
# where bits[i] = count of i-bit codes (slot 0 unused); vals = symbols
# in canonical code order.
_HDC_LUMA = ([
    0, 0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0,
    ],
    [
        0x0, 0x1, 0x2, 0x3, 0x4, 0x5, 0x6, 0x7,
        0x8, 0x9, 0xa, 0xb
    ])

_HDC_CHR = ([
    0, 0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0,
    ],
    [
        0x0, 0x1, 0x2, 0x3, 0x4, 0x5, 0x6, 0x7,
        0x8, 0x9, 0xa, 0xb
    ])

_HAC_LUMA = ([
    0, 0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 125,
    ],
    [
        0x1, 0x2, 0x3, 0x0, 0x4, 0x11, 0x5, 0x12,
        0x21, 0x31, 0x41, 0x6, 0x13, 0x51, 0x61, 0x7,
        0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xa1, 0x8,
        0x23, 0x42, 0xb1, 0xc1, 0x15, 0x52, 0xd1, 0xf0,
        0x24, 0x33, 0x62, 0x72, 0x82, 0x9, 0xa, 0x16,
        0x17, 0x18, 0x19, 0x1a, 0x25, 0x26, 0x27, 0x28,
        0x29, 0x2a, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39,
        0x3a, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49,
        0x4a, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
        0x5a, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69,
        0x6a, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78, 0x79,
        0x7a, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
        0x8a, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98,
        0x99, 0x9a, 0xa2, 0xa3, 0xa4, 0xa5, 0xa6, 0xa7,
        0xa8, 0xa9, 0xaa, 0xb2, 0xb3, 0xb4, 0xb5, 0xb6,
        0xb7, 0xb8, 0xb9, 0xba, 0xc2, 0xc3, 0xc4, 0xc5,
        0xc6, 0xc7, 0xc8, 0xc9, 0xca, 0xd2, 0xd3, 0xd4,
        0xd5, 0xd6, 0xd7, 0xd8, 0xd9, 0xda, 0xe1, 0xe2,
        0xe3, 0xe4, 0xe5, 0xe6, 0xe7, 0xe8, 0xe9, 0xea,
        0xf1, 0xf2, 0xf3, 0xf4, 0xf5, 0xf6, 0xf7, 0xf8,
        0xf9, 0xfa
    ])

_HAC_CHR = ([
    0, 0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 119,
    ],
    [
        0x0, 0x1, 0x2, 0x3, 0x11, 0x4, 0x5, 0x21,
        0x31, 0x6, 0x12, 0x41, 0x51, 0x7, 0x61, 0x71,
        0x13, 0x22, 0x32, 0x81, 0x8, 0x14, 0x42, 0x91,
        0xa1, 0xb1, 0xc1, 0x9, 0x23, 0x33, 0x52, 0xf0,
        0x15, 0x62, 0x72, 0xd1, 0xa, 0x16, 0x24, 0x34,
        0xe1, 0x25, 0xf1, 0x17, 0x18, 0x19, 0x1a, 0x26,
        0x27, 0x28, 0x29, 0x2a, 0x35, 0x36, 0x37, 0x38,
        0x39, 0x3a, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48,
        0x49, 0x4a, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58,
        0x59, 0x5a, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68,
        0x69, 0x6a, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78,
        0x79, 0x7a, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87,
        0x88, 0x89, 0x8a, 0x92, 0x93, 0x94, 0x95, 0x96,
        0x97, 0x98, 0x99, 0x9a, 0xa2, 0xa3, 0xa4, 0xa5,
        0xa6, 0xa7, 0xa8, 0xa9, 0xaa, 0xb2, 0xb3, 0xb4,
        0xb5, 0xb6, 0xb7, 0xb8, 0xb9, 0xba, 0xc2, 0xc3,
        0xc4, 0xc5, 0xc6, 0xc7, 0xc8, 0xc9, 0xca, 0xd2,
        0xd3, 0xd4, 0xd5, 0xd6, 0xd7, 0xd8, 0xd9, 0xda,
        0xe2, 0xe3, 0xe4, 0xe5, 0xe6, 0xe7, 0xe8, 0xe9,
        0xea, 0xf2, 0xf3, 0xf4, 0xf5, 0xf6, 0xf7, 0xf8,
        0xf9, 0xfa
    ])
def _build_huff(bits, syms):
    """Canonical Huffman code/length lookup by symbol."""
    code = {}
    length = {}
    val = 0
    k = 0
    for i in range(1, 17):
        for _ in range(bits[i]):
            code[syms[k]] = val
            length[syms[k]] = i
            val += 1
            k += 1
        val <<= 1
    return code, length


_LUM_DC_CODE, _LUM_DC_LEN = _build_huff(*_HDC_LUMA)
_CHR_DC_CODE, _CHR_DC_LEN = _build_huff(*_HDC_CHR)
_LUM_AC_CODE, _LUM_AC_LEN = _build_huff(*_HAC_LUMA)
_CHR_AC_CODE, _CHR_AC_LEN = _build_huff(*_HAC_CHR)

_DC_CODE = [_LUM_DC_CODE, _CHR_DC_CODE, _CHR_DC_CODE]
_DC_LEN = [_LUM_DC_LEN, _CHR_DC_LEN, _CHR_DC_LEN]
_AC_CODE = [_LUM_AC_CODE, _CHR_AC_CODE, _CHR_AC_CODE]
_AC_LEN = [_LUM_AC_LEN, _CHR_AC_LEN, _CHR_AC_LEN]
# In the IJG/libjpeg convention 0xF0 is used for BOTH EOB and ZRL (the decoder
# advances 16 zeros per 0xF0 and ends the block when the position passes 63).
# 0x00 is a single zero coefficient. So EOB and ZRL share the 0xF0 code.
_EOB_CODE = [_LUM_AC_CODE[0x00], _CHR_AC_CODE[0x00], _CHR_AC_CODE[0x00]]
_EOB_LEN = [_LUM_AC_LEN[0x00], _CHR_AC_LEN[0x00], _CHR_AC_LEN[0x00]]
_ZRL_CODE = [_LUM_AC_CODE[0xF0], _CHR_AC_CODE[0xF0], _CHR_AC_CODE[0xF0]]
_ZRL_LEN = [_LUM_AC_LEN[0xF0], _CHR_AC_LEN[0xF0], _CHR_AC_LEN[0xF0]]


def _scale_quant(table, quality):
    q = max(1, min(100, quality))
    if q < 50:
        scale = 5000 // q
    else:
        scale = 200 - 2 * q
    out = []
    for v in table:
        v = (v * scale + 50) // 100
        out.append(max(1, min(255, v)))
    return out


import math as _math  # noqa: E402


def _dct_matrix():
    m = [[0.0] * 8 for _ in range(8)]
    for u in range(8):
        c = 1.0 / _math.sqrt(2.0) if u == 0 else 1.0
        for x in range(8):
            m[u][x] = c * 0.5 * _math.cos((2 * x + 1) * u * _math.pi / 16.0)
    return m


_FCT = _dct_matrix()
_FCTT = [[_FCT[x][u] for x in range(8)] for u in range(8)]


def _fdct_block(block):
    """block: 64 floats, raster order -> 8x8 DCT coefficients."""
    # The 2D orthonormal DCT is F = T * f * T^T (T is not symmetric).
    # Pass 1: tmp = T * f  (transform each column of f on the left by T).
    tmp = [[0.0] * 8 for _ in range(8)]
    for u in range(8):
        fr = _FCT[u]
        for x in range(8):
            s = 0.0
            for v in range(8):
                s += fr[v] * block[v * 8 + x]
            tmp[u][x] = s
    # Pass 2: F = tmp * T^T  (transform each row of tmp on the right by T^T),
    # i.e. F[u][x] = sum_v tmp[u][v] * T[x][v].
    out = [[0.0] * 8 for _ in range(8)]
    for u in range(8):
        row = tmp[u]
        for x in range(8):
            fc = _FCT[x]
            s = 0.0
            for v in range(8):
                s += row[v] * fc[v]
            out[u][x] = s
    return out


def _rgb_to_ycbcr(px):
    # JFIF convention: Y is CENTERED (subtract 128) before the DCT so the
    # decoder's +128 recovers [0,255]. Cb/Cr are already centered on 0.
    r, g, b = px[0], px[1], px[2]
    y = 0.299 * r + 0.587 * g + 0.114 * b - 128.0
    cb = -0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 0.5 * r - 0.418688 * g - 0.081312 * b
    return y, cb, cr


def _dht_marker(class_id, bits, syms):
    # DHT stores 16 code-length counts (slots 1..16); bits is a 17-slot list
    # whose slot 0 is unused, so drop it before writing.
    d = bytes([class_id]) + bytes(bits[1:17]) + bytes(syms)
    return b"\xFF\xC4" + struct.pack(">H", len(d) + 2) + d


def encode_jpeg(rgb: bytes, w: int, h: int, quality: int = 80) -> bytes:
    """Encode RGB8 top-down rows as a baseline JFIF JPEG file (4:4:4)."""
    qtables = [_scale_quant(STD_LUM_QUANT, quality),
               _scale_quant(STD_CHR_QUANT, quality),
               _scale_quant(STD_CHR_QUANT, quality)]

    pw = (w + 7) // 8 * 8
    ph = (h + 7) // 8 * 8
    # All planes (Y and Cb/Cr centered on 0) pad to 0 for incomplete blocks.
    planes = [[0.0] * (pw * ph) for _ in range(3)]
    for y in range(h):
        ro = y * w * 3
        po = y * pw
        for x in range(w):
            v, cb, cr = _rgb_to_ycbcr(rgb[ro + x * 3 : ro + x * 3 + 3])
            idx = po + x
            planes[0][idx] = v
            planes[1][idx] = cb
            planes[2][idx] = cr

    # ---- header ---------------------------------------------------------
    hdr = bytearray()
    hdr += b"\xFF\xD8"
    app0 = b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    hdr += b"\xFF\xE0" + struct.pack(">H", len(app0) + 2) + app0
    # DQT segment is in zigzag order; the standard tables are stored in raster
    # order, so permute them into zigzag order (entry i <- raster ZIGZAG[i]).
    for tid, qt in ((0x00, qtables[0]), (0x01, qtables[1])):
        zz_qt = bytes(qt[ZIGZAG[i]] for i in range(64))
        hdr += b"\xFF\xDB" + struct.pack(">H", 2 + 1 + 64) + bytes([tid]) + zz_qt
    ncomp = 3
    hdr += b"\xFF\xC0" + struct.pack(">HBHHB", 8 + 3 * ncomp, 8, ph, pw, ncomp)
    hdr += b"\x01\x11\x00"
    hdr += b"\x02\x11\x01"
    hdr += b"\x03\x11\x01"
    hdr += _dht_marker(0x00, *_HDC_LUMA)
    hdr += _dht_marker(0x10, *_HAC_LUMA)
    hdr += _dht_marker(0x01, *_HDC_CHR)
    hdr += _dht_marker(0x11, *_HAC_CHR)
    # Per-component Xsj: luma=0x00, chroma=0x11 (matches libjpeg-turbo). The
    # global Ss/Se (0x00/0x3F) below is the authoritative spectral selection.
    sos = b"\x03" + b"\x01\x00" + b"\x02\x11" + b"\x03\x11" + b"\x00\x3F\x00"
    hdr += b"\xFF\xDA" + struct.pack(">H", len(sos) + 2) + sos

    # ---- entropy-coded data --------------------------------------------
    bits_out = bytearray()
    acc = 0
    nbits = 0

    def put(code, n):
        nonlocal bits_out, acc, nbits
        if n == 0:
            return
        acc = (acc << n) | (code & ((1 << n) - 1))
        nbits += n
        while nbits >= 8:
            nbits -= 8
            b = (acc >> nbits) & 0xFF
            bits_out.append(b)
            if b == 0xFF:
                bits_out.append(0x00)

    def flush():
        nonlocal acc, nbits
        if nbits:
            acc = (acc << (8 - nbits)) & 0xFF
            bits_out.append(acc)
            if acc == 0xFF:
                bits_out.append(0x00)
            acc = 0
            nbits = 0

    prev_dc = [0, 0, 0]
    # JPEG scans MCUs in raster (row-major) order: y outer, x inner.
    for r in range(0, ph, 8):
        for c in range(0, pw, 8):
            for comp in range(3):
                base = r * pw + c
                blk = [planes[comp][base + y * pw + x] for y in range(8) for x in range(8)]
                f = _fdct_block(blk)
                qt = qtables[comp]
                zz = [0] * 64
                for i in range(64):
                    ri = ZIGZAG[i]
                    # qt is stored in raster order, so the quantizer for the
                    # coefficient at raster index `ri` is qt[ri] (the DQT is
                    # emitted in zigzag order to match the bitstream).
                    v = f[ri // 8][ri % 8] / qt[ri]
                    iv = int(v + 0.5) if v >= 0 else -int(-v + 0.5)
                    zz[i] = iv
                diff = zz[0] - prev_dc[comp]
                prev_dc[comp] = zz[0]
                # JPEG two's-complement DC coding: category = abs(diff).bit_length()
                # (category 0 for diff 0); negative payloads are two's complement,
                # i.e. diff + (1 << cat) - 1.
                if diff == 0:
                    cat = 0
                    payload = 0
                elif diff > 0:
                    cat = diff.bit_length()
                    payload = diff
                else:
                    cat = (-diff).bit_length()
                    payload = diff + (1 << cat) - 1
                put(_DC_CODE[comp][cat], _DC_LEN[comp][cat])
                if cat:
                    put(payload & ((1 << cat) - 1), cat)
                R = 0
                i = 1
                while i <= 63:
                    if zz[i] == 0:
                        R += 1
                        i += 1
                        # Emit 0xF0 (ZRL) for every full run of 16 zeros.
                        while R >= 16:
                            put(_AC_CODE[comp][0xF0], _AC_LEN[comp][0xF0])
                            R -= 16
                    else:
                        v = zz[i]
                        # two's-complement size = abs(v).bit_length()
                        if v > 0:
                            bits = v.bit_length()
                        else:
                            bits = (-v).bit_length()
                        sym = (R << 4) | bits
                        put(_AC_CODE[comp][sym], _AC_LEN[comp][sym])
                        payload = v if v > 0 else v + (1 << bits) - 1
                        put(payload & ((1 << bits) - 1), bits)
                        R = 0
                        i += 1
                # EOB (0x00) covers the trailing zeros (R < 16) if any remain.
                if R > 0:
                    put(_EOB_CODE[comp], _EOB_LEN[comp])
    flush()

    hdr += bytes(bits_out)
    hdr += b"\xFF\xD9"
    return bytes(hdr)


def save_image(out_path: str, rgb: bytes, w: int, h: int, fmt: str, quality: int = 80) -> int:
    """Encode RGB8 top-down rows to `out_path` as png or jpeg. Returns size."""
    if fmt == "png":
        data = encode_png(rgb, w, h)
    elif fmt in ("jpeg", "jpg"):
        data = encode_jpeg(rgb, w, h, quality)
    else:
        raise ValueError(f"unknown fmt {fmt!r}")
    with open(out_path, "wb") as f:
        f.write(data)
    return len(data)