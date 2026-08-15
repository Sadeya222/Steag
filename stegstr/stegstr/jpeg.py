"""Minimal baseline-JPEG reader/writer — the engine's native transport.

Why a hand-rolled JPEG codec?  The Tier-A engine embeds into the *quantized
coefficient levels* of a JPEG (JSteg-lineage).  That means:

- writing: we control the exact quantized levels in the carrier file
  (parity of a level is the payload bit);
- reading: we parse the *received* JPEG's entropy stream and read the
  platform's quantized levels directly — no pixel-domain FDCT at all.

That removes every error source that plagues pixel-domain re-DCT
steganography: uint8 rounding losses (~10-30% for small coefficients), the
DCT-implementation mismatch between numpy and libjpeg, color-conversion
noise, and dead-zone pulls.  The only residual error is the platform's own
decode->encode consistency (well under one level), which the redundant
voting + RS absorbs trivially.

Scope: baseline JPEG only (progressive is a later milestone).  The reader
handles 4:4:4 and 4:2:0, interleaved and non-interleaved scans, DRI/RST
markers, and any DHT/DQT tables present in the file.  The writer emits
4:4:4 baseline with the standard Annex-K Huffman tables and a caller-supplied
quantization table.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

# --------------------------------------------------------------------------
# standard Huffman tables (JPEG Annex K) — used by the writer
# --------------------------------------------------------------------------
_LUM_DC_BITS = [0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0]
_LUM_DC_VALS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
_CHR_DC_BITS = [0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0]
_CHR_DC_VALS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
_LUM_AC_BITS = [0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7D]
_CHR_AC_BITS = [0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 0x77]
_LUM_AC_VALS = [
    0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06,
    0x13, 0x51, 0x61, 0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08,
    0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0, 0x24, 0x33, 0x62, 0x72,
    0x82, 0x09, 0x0A, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
    0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45,
    0x46, 0x47, 0x48, 0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
    0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x73, 0x74, 0x75,
    0x76, 0x77, 0x78, 0x79, 0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
    0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3,
    0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6,
    0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9,
    0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
    0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF1, 0xF2, 0xF3, 0xF4,
    0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0xFA,
]
_CHR_AC_VALS = [
    0x00, 0x01, 0x02, 0x03, 0x11, 0x04, 0x05, 0x21, 0x31, 0x06, 0x12, 0x41,
    0x51, 0x07, 0x61, 0x71, 0x13, 0x22, 0x32, 0x81, 0x08, 0x14, 0x42, 0x91,
    0xA1, 0xB1, 0xC1, 0x09, 0x23, 0x33, 0x52, 0xF0, 0x15, 0x62, 0x72, 0xD1,
    0x0A, 0x16, 0x24, 0x34, 0xE1, 0x25, 0xF1, 0x17, 0x18, 0x19, 0x1A, 0x26,
    0x27, 0x28, 0x29, 0x2A, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44,
    0x45, 0x46, 0x47, 0x48, 0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58,
    0x59, 0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x73, 0x74,
    0x75, 0x76, 0x77, 0x78, 0x79, 0x7A, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87,
    0x88, 0x89, 0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A,
    0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4,
    0xB5, 0xB6, 0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7,
    0xC8, 0xC9, 0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA,
    0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF2, 0xF3, 0xF4,
    0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0xFA,
]


def _build_huff_tables(bits: list[int], vals: list[int]) -> tuple[dict, dict]:
    """Build (encode: symbol->(code,length), decode: code->symbol) tables."""
    code = 0
    enc: dict[int, tuple[int, int]] = {}
    dec: dict[tuple[int, int], int] = {}
    k = 0
    for length in range(1, 17):
        for _ in range(bits[length - 1]):
            sym = vals[k]
            enc[sym] = (code, length)
            dec[(code, length)] = sym
            code += 1
            k += 1
        code <<= 1
    return enc, dec


STD_TABLES = {
    # (table_class, table_id) -> (encode, decode)
    (0, 0): _build_huff_tables(_LUM_DC_BITS, _LUM_DC_VALS),
    (1, 0): _build_huff_tables(_LUM_AC_BITS, _LUM_AC_VALS),
    (0, 1): _build_huff_tables(_CHR_DC_BITS, _CHR_DC_VALS),
    (1, 1): _build_huff_tables(_CHR_AC_BITS, _CHR_AC_VALS),
}


# --------------------------------------------------------------------------
# reader
# --------------------------------------------------------------------------
@dataclass
class JPEG:
    height: int
    width: int
    components: list[dict]              # {id, h, v, dc_table, ac_table, quant_table}
    quant_tables: dict[int, np.ndarray]  # id -> 8x8
    blocks: dict[int, np.ndarray]        # component id -> (n_blocks, 8, 8) int64 levels
    huff_dc: dict = field(default_factory=dict)   # (table_id) -> decode table
    huff_ac: dict = field(default_factory=dict)
    mcu_layout: list[tuple] = field(default_factory=list)  # (component_id, h, v) per MCU

    @property
    def is_grayscale(self) -> bool:
        return len(self.components) == 1


class JpegError(Exception):
    pass


def _read_segments(data: bytes):
    """Yield (marker, payload) for each segment up to SOS."""
    i = 0
    assert data[:2] == b"\xff\xd8", "not a JPEG"
    i = 2
    while i < len(data):
        while data[i] != 0xFF:
            i += 1
        marker = data[i + 1]
        if marker == 0xD8:  # SOI
            i += 2
            continue
        if marker == 0xD9:  # EOI
            yield marker, b"", i
            return
        length = struct.unpack(">H", data[i + 2 : i + 4])[0]
        yield marker, data[i + 4 : i + 2 + length], i
        if marker == 0xDA:  # SOS — entropy data follows the segment
            yield marker, data[i + 4 : i + 2 + length], i + 2 + length
            return
        i += 2 + length


def parse_jpeg(data: bytes) -> JPEG:
    """Parse a baseline JPEG into levels (quantized DCT coefficients)."""
    sos_payload = None
    sos_pos = 0
    frame = None
    quant_tables: dict[int, np.ndarray] = {}
    huff_dc: dict[int, tuple] = {}
    huff_ac: dict[int, tuple] = {}
    for marker, payload, pos in _read_segments(data):
        if marker == 0xDB:  # DQT
            p = 0
            while p < len(payload):
                pq, tid = payload[p] >> 4, payload[p] & 0x0F
                n = 64 if pq == 0 else 128
                raw = payload[p + 1 : p + 1 + n]
                table = np.zeros(64, dtype=np.int64)
                for k in range(64):
                    table[k] = raw[k] if pq == 0 else struct.unpack(">H", raw[2 * k : 2 * k + 2])[0]
                quant_tables[tid] = _unzigzag(table)
                p += 1 + n
        elif marker == 0xC0 or marker == 0xC1 or marker == 0xC2:  # SOF0/1/2
            if marker != 0xC0:
                raise JpegError("progressive/other JPEG not supported yet (only baseline)")
            precision = payload[0]
            height = struct.unpack(">H", payload[1:3])[0]
            width = struct.unpack(">H", payload[3:5])[0]
            ncomp = payload[5]
            comps = []
            for c in range(ncomp):
                cid = payload[6 + 3 * c]
                hv = payload[7 + 3 * c]
                tq = payload[8 + 3 * c]
                comps.append({
                    "id": cid, "h": hv >> 4, "v": hv & 0x0F,
                    "dc_table": 0, "ac_table": 0, "quant_table": tq,
                })
            frame = (precision, height, width, comps)
        elif marker == 0xC4:  # DHT
            p = 0
            while p < len(payload):
                tc_th = payload[p]
                tc, th = tc_th >> 4, tc_th & 0x0F
                bits = list(payload[p + 1 : p + 17])
                n = sum(bits)
                vals = list(payload[p + 17 : p + 17 + n])
                enc, dec = _build_huff_tables(bits, vals)
                if tc == 0:
                    huff_dc[th] = dec
                else:
                    huff_ac[th] = dec
                p += 17 + n
        elif marker == 0xDA:  # SOS — handled after loop
            sos_payload = payload
            sos_pos = pos
    if frame is None:
        raise JpegError("no SOF found")
    if sos_payload is None:
        raise JpegError("no SOS found")
    precision, height, width, comps = frame

    # scan header
    ns = sos_payload[0]
    comp_ids = [sos_payload[1 + 2 * i] for i in range(ns)]
    comp_tables = {
        sos_payload[1 + 2 * i]: (sos_payload[2 + 2 * i] >> 4, sos_payload[2 + 2 * i] & 0x0F)
        for i in range(ns)
    }
    for c in comps:
        c["dc_table"], c["ac_table"] = comp_tables.get(c["id"], (0, 0))

    # MCU layout
    max_h = max(c["h"] for c in comps)
    max_v = max(c["v"] for c in comps)
    if ns == 1 and max_h == 1 and max_v == 1:
        mcus_x = (width + 7) // 8
        mcus_y = (height + 7) // 8
        layout = [(comps[0]["id"], 1, 1)]
    else:
        mcus_x = (width + max_h * 8 - 1) // (max_h * 8)
        mcus_y = (height + max_v * 8 - 1) // (max_v * 8)
        layout = [(c["id"], c["h"], c["v"]) for c in comps]

    n_blocks = {
        c["id"]: mcus_x * mcus_y * c["h"] * c["v"] for c in comps
    }
    blocks = {c["id"]: np.zeros((n_blocks[c["id"]], 64), dtype=np.int64) for c in comps}

    # entropy decode
    i = sos_pos  # start of entropy data (right after the SOS segment)
    bitbuf = 0
    bitcnt = 0
    byte = 0
    pred = {c["id"]: 0 for c in comps}
    block_idx = {c["id"]: 0 for c in comps}
    seen_eoi = False

    def next_byte():
        nonlocal i
        if i >= len(data):
            raise JpegError("truncated entropy data")
        b = data[i]
        i += 1
        if b == 0xFF:
            b2 = data[i]
            i += 1
            if b2 == 0x00:
                return 0xFF
            if 0xD0 <= b2 <= 0xD7:  # RSTn
                return next_byte()
            if b2 == 0xD9:  # EOI
                nonlocal seen_eoi
                seen_eoi = True
                return 0xFF
            raise JpegError(f"unexpected marker 0xFF{b2:02X} in entropy data")
        return b

    def get_bits(n: int) -> int:
        nonlocal bitbuf, bitcnt, byte
        while bitcnt < n:
            byte = next_byte()
            bitbuf = (bitbuf << 8) | byte
            bitcnt += 8
        bitcnt -= n
        return (bitbuf >> bitcnt) & ((1 << n) - 1)

    def huff_decode(dec: dict) -> int:
        code = 0
        for length in range(1, 17):
            code = (code << 1) | get_bits(1)
            sym = dec.get((code, length))
            if sym is not None:
                return sym
        raise JpegError("bad huffman code")

    def receive_extend(s: int) -> int:
        if s == 0:
            return 0
        v = get_bits(s)
        if v < (1 << (s - 1)):
            v -= (1 << s) - 1
        return v

    total_blocks = sum(n_blocks.values())
    decoded_blocks = 0
    while not seen_eoi and decoded_blocks < total_blocks:
        for cid, h, v in layout:
            comp = _table_for(comps, cid)
            dc_dec = huff_dc.get(comp["dc_table"], _STD_DC_DEC.get(comp["dc_table"]))
            ac_dec = huff_ac.get(comp["ac_table"], _STD_AC_DEC.get(comp["ac_table"]))
            for _ in range(h * v):
                if block_idx[cid] >= n_blocks[cid]:
                    break
                blk = blocks[cid][block_idx[cid]]
                t = huff_decode(dc_dec)
                diff = receive_extend(t)
                pred[cid] += diff
                blk[0] = pred[cid]
                k = 1
                while k < 64:
                    rs = huff_decode(ac_dec)
                    r, s = rs >> 4, rs & 0x0F
                    if s == 0:
                        if r == 15:
                            k += 16
                            continue
                        break  # EOB
                    k += r
                    if k > 63:
                        raise JpegError("AC run overflow")
                    blk[k] = receive_extend(s)
                    k += 1
                block_idx[cid] += 1
                decoded_blocks += 1

    jpeg = JPEG(
        height=height, width=width, components=comps,
        quant_tables=quant_tables, blocks=blocks,
        huff_dc=huff_dc, huff_ac=huff_ac, mcu_layout=layout,
    )
    return jpeg


def _table_for(comps, cid):
    for c in comps:
        if c["id"] == cid:
            return c
    raise JpegError(f"component {cid} not in frame")


# standard tables used as fallback when a file carries no DHT (shouldn't happen)
_STD_DC_DEC = {0: STD_TABLES[(0, 0)][1], 1: STD_TABLES[(0, 1)][1]}
_STD_AC_DEC = {0: STD_TABLES[(1, 0)][1], 1: STD_TABLES[(1, 1)][1]}


# --------------------------------------------------------------------------
# writer
# --------------------------------------------------------------------------
def _zigzag_table() -> np.ndarray:
    """(r,c) -> zigzag index, and inverse."""
    zz = []
    r = c = 0
    down = True
    for _ in range(64):
        zz.append((r, c))
        if down:
            if c == 7:
                r += 1; down = False
            elif r == 0:
                c += 1; down = False
            else:
                r -= 1; c += 1
        else:
            if r == 7:
                c += 1; down = True
            elif c == 0:
                r += 1; down = True
            else:
                r += 1; c -= 1
    return np.array(zz)


_ZZ = _zigzag_table()


def _unzigzag(flat: np.ndarray) -> np.ndarray:
    """DQT flat (zigzag order) -> 8x8 natural order."""
    out = np.zeros((8, 8), dtype=np.int64)
    for k in range(64):
        r, c = _ZZ[k]
        out[r, c] = flat[k]
    return out


def _quant_flat(table: np.ndarray) -> bytes:
    """8x8 table -> DQT payload (zigzag order, 8-bit precision)."""
    out = bytearray()
    for k in range(64):
        r, c = _ZZ[k]
        out.append(int(np.clip(table[r, c], 1, 255)))
    return bytes(out)


def _huff_bytes(bits: list[int], vals: list[int]) -> bytes:
    return bytes(bits) + bytes(vals)


class _BitWriter:
    def __init__(self):
        self.out = bytearray()
        self.acc = 0
        self.n = 0

    def put(self, value: int, nbits: int) -> None:
        for b in range(nbits - 1, -1, -1):
            self.acc = (self.acc << 1) | ((value >> b) & 1)
            self.n += 1
            if self.n == 8:
                self._flush_byte()

    def _flush_byte(self) -> None:
        b = self.acc & 0xFF
        self.out.append(b)
        if b == 0xFF:
            self.out.append(0x00)
        self.acc = 0
        self.n = 0

    def finish(self) -> bytes:
        if self.n:
            self.acc <<= (8 - self.n)
            self._flush_byte()
        return bytes(self.out)


def _extend(value: int, s: int) -> int:
    """Amplitude bits for a value with category s."""
    if s == 0:
        return 0
    if value > 0:
        return value
    return value + ((1 << s) - 1)


def _category(value: int) -> int:
    return int(np.abs(value)).bit_length()


def write_jpeg(
    blocks: dict[int, np.ndarray],
    quant_tables: dict[int, np.ndarray],
    width: int,
    height: int,
    component_ids: tuple[int, ...] = (1, 2, 3),
) -> bytes:
    """Write a baseline 4:4:4 JPEG with standard Huffman tables.

    ``blocks``: component id -> (n_blocks, 64) quantized levels (zigzag order,
    DC at index 0, no prediction applied).
    ``quant_tables``: id -> 8x8 table (natural order) written into DQT.
    """
    ncomp = len(component_ids)
    assert ncomp in (1, 3)
    assert width % 8 == 0 and height % 8 == 0, "dimensions must be multiples of 8"
    mcus_x = width // 8
    mcus_y = height // 8

    out = bytearray()
    out += b"\xff\xd8"  # SOI
    # APP0 JFIF
    app0 = b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    out += b"\xff\xe0" + struct.pack(">H", len(app0) + 2) + app0
    # DQT
    for tid in sorted(quant_tables):
        payload = bytes([0x00 | tid]) + _quant_flat(quant_tables[tid])
        out += b"\xff\xdb" + struct.pack(">H", len(payload) + 2) + payload
    # SOF0
    sof = bytes([8]) + struct.pack(">HH", height, width) + bytes([ncomp])
    for i, cid in enumerate(component_ids):
        sof += bytes([cid, 0x11, 0 if i == 0 else 1])
    out += b"\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof
    # DHT (standard tables)
    dht = _huff_bytes(_LUM_DC_BITS, _LUM_DC_VALS)
    out += b"\xff\xc4" + struct.pack(">H", len(dht) + 2 + 1) + bytes([0x00]) + dht
    dht = _huff_bytes(_LUM_AC_BITS, _LUM_AC_VALS)
    out += b"\xff\xc4" + struct.pack(">H", len(dht) + 2 + 1) + bytes([0x10]) + dht
    if ncomp == 3:
        dht = _huff_bytes(_CHR_DC_BITS, _CHR_DC_VALS)
        out += b"\xff\xc4" + struct.pack(">H", len(dht) + 2 + 1) + bytes([0x01]) + dht
        dht = _huff_bytes(_CHR_AC_BITS, _CHR_AC_VALS)
        out += b"\xff\xc4" + struct.pack(">H", len(dht) + 2 + 1) + bytes([0x11]) + dht
    # SOS
    sos = bytes([ncomp])
    for i, cid in enumerate(component_ids):
        sos += bytes([cid, 0x00 if i == 0 else 0x11])  # dc_table<<4 | ac_table
    sos += b"\x00\x3f\x00"
    out += b"\xff\xda" + struct.pack(">H", len(sos) + 2) + sos

    bw = _BitWriter()
    pred = {cid: 0 for cid in component_ids}
    for mcu_y in range(mcus_y):
        for mcu_x in range(mcus_x):
            for cid in component_ids:
                blk_idx = mcu_y * mcus_x + mcu_x
                blk = blocks[cid][blk_idx]
                # DC
                diff = int(blk[0]) - pred[cid]
                pred[cid] = int(blk[0])
                s = _category(diff)
                enc, _ = STD_TABLES[(0, 0 if cid == component_ids[0] else 1)]
                code, length = enc[s]
                bw.put(code, length)
                bw.put(_extend(diff, s), s)
                # AC
                enc, _ = STD_TABLES[(1, 0 if cid == component_ids[0] else 1)]
                k = 1
                while k < 64:
                    run = 0
                    while k < 64 and blk[k] == 0:
                        k += 1
                        run += 1
                        if run == 16:
                            code, length = enc[0xF0]
                            bw.put(code, length)
                            run = 0
                    if k >= 64:
                        code, length = enc[0x00]  # EOB
                        bw.put(code, length)
                        break
                    s = _category(blk[k])
                    sym = (run << 4) | s
                    code, length = enc[sym]
                    bw.put(code, length)
                    bw.put(_extend(int(blk[k]), s), s)
                    k += 1
    out += bw.finish()
    out += b"\xff\xd9"  # EOI
    return bytes(out)


# --------------------------------------------------------------------------
# convenience: quantized levels <-> pixels via the spec DCT (for channel
# previews and for building carriers from PIL images)
# --------------------------------------------------------------------------
def levels_to_ycbcr(blocks: dict[int, np.ndarray], quant_tables: dict[int, np.ndarray],
                    width: int, height: int) -> np.ndarray:
    """Reconstruct (h, w, 3) uint8 RGB from quantized levels (4:4:4 layout)."""
    from .dct import idct
    planes = {}
    for cid, name in ((1, "Y"), (2, "Cb"), (3, "Cr")):
        qt = quant_tables[0 if cid == 1 else 1]
        blks = blocks[cid].reshape(height // 8, width // 8, 8, 8)
        coeffs = blks * qt
        planes[name] = np.clip(np.round(idct(coeffs)), 0, 255).reshape(height, width).astype(np.uint8)
    rgb = ycbcr_to_rgb(planes["Y"], planes["Cb"], planes["Cr"])
    return rgb


def ycbcr_to_rgb(y: np.ndarray, cb: np.ndarray, cr: np.ndarray) -> np.ndarray:
    """BT.601 full-range YCbCr -> RGB (uint8)."""
    from .colors import ycbcr_to_rgb as _convert
    return _convert(y, cb, cr)


def rgb_to_ycbcr(rgb: np.ndarray):
    from .colors import rgb_to_ycbcr as _convert
    return _convert(rgb)
