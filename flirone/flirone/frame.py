"""Wire format of the FLIR One frame stream and raw-count -> temperature conversion.

Everything here is pure Python/numpy (no USB), so it can be unit-tested and
re-used to analyse saved recordings.

Frame layout on the "frame" bulk endpoint (little endian):

    offset  size  meaning
    0       4     magic  EF BE 00 00
    4       4     (unused here)
    8       4     FrameSize   = bytes that follow the 28-byte header
    12      4     ThermalSize = bytes of raw thermal data
    16      4     JpgSize     = bytes of the visible-camera JPEG
    20      4     StatusSize  = bytes of a JSON status string
    24      4     (unused here)
    28      ...   thermal block, then JPEG, then status JSON

Thermal block, 160x120 sensor (FLIR One Gen 2, FLIR One Pro):
    120 rows, each 164 uint16 words = [2 words][80 pixels][2 words][80 pixels]
Thermal block, 80x60 sensor (FLIR One Gen 1, Gen 3, Pro LT):
    60 rows, each 82 uint16 words = [2 words][80 pixels]
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field

import numpy as np

MAGIC = b"\xef\xbe\x00\x00"
HEADER_LEN = 28
MAX_FRAME = 4 * 1024 * 1024  # sanity limit on FrameSize

# name -> (width, height, words per row, list of (start_word, stop_word) pixel runs)
LAYOUTS = {
    "160x120": (160, 120, 164, [(2, 82), (84, 164)]),
    "80x60": (80, 60, 82, [(2, 82)]),
}


class FrameError(ValueError):
    pass


@dataclass
class Frame:
    raw: np.ndarray                 # uint16 (H, W) sensor counts
    jpeg: bytes = b""               # visible camera image (may be empty)
    status: dict = field(default_factory=dict)
    layout: str = ""
    timestamp: float = 0.0          # seconds, host clock (time.perf_counter based)
    wire: bytes = b""               # the complete frame as received (for debugging)

    @property
    def ffc(self) -> bool:
        """True while the camera's internal shutter calibration is running.
        Frames taken during it show the shutter, not the scene."""
        return str(self.status.get("shutterState", "")).upper().startswith("FFC")


def guess_layout(thermal_size: int) -> str:
    """Pick the sensor layout from the size of the thermal block."""
    best = None
    for name, (w, h, row_words, _runs) in LAYOUTS.items():
        need = row_words * h * 2
        if thermal_size >= need and (best is None or need > best[1]):
            best = (name, need)
    if best is None:
        raise FrameError(
            f"Thermal block of {thermal_size} bytes matches no known sensor layout "
            f"(expected >= 9840 for 80x60 or >= 39360 for 160x120)."
        )
    return best[0]


def decode_thermal(block: bytes, layout: str | None = None) -> tuple[np.ndarray, str]:
    layout = layout or guess_layout(len(block))
    w, h, row_words, runs = LAYOUTS[layout]
    need = row_words * h * 2
    if len(block) < need:
        raise FrameError(f"Thermal block too short for {layout}: {len(block)} < {need}")
    rows = np.frombuffer(block, dtype="<u2", count=row_words * h).reshape(h, row_words)
    img = np.concatenate([rows[:, a:b] for a, b in runs], axis=1)
    return np.ascontiguousarray(img), layout


def parse_frame(buf: bytes, layout: str | None = None, timestamp: float = 0.0) -> Frame:
    if len(buf) < HEADER_LEN or buf[:4] != MAGIC:
        raise FrameError("Not a FLIR One frame (bad magic)")
    frame_size, thermal_size, jpg_size, status_size = struct.unpack_from("<4I", buf, 8)
    if len(buf) < HEADER_LEN + frame_size:
        raise FrameError("Frame incomplete")
    if thermal_size + jpg_size + status_size > frame_size:
        raise FrameError(
            f"Inconsistent header: thermal {thermal_size} + jpg {jpg_size} + "
            f"status {status_size} > frame {frame_size}"
        )
    p = HEADER_LEN
    raw, used = decode_thermal(buf[p:p + thermal_size], layout)
    p += thermal_size
    jpeg = bytes(buf[p:p + jpg_size])
    p += jpg_size
    status_bytes = bytes(buf[p:p + status_size])
    try:
        status = json.loads(status_bytes.split(b"\x00", 1)[0].decode("utf-8", "replace") or "{}")
        if not isinstance(status, dict):
            status = {"value": status}
    except ValueError:
        status = {"unparsed": status_bytes[:200].decode("latin1")}
    return Frame(raw=raw, jpeg=jpeg, status=status, layout=used, timestamp=timestamp,
                 wire=bytes(buf[:HEADER_LEN + frame_size]))


class FrameAssembler:
    """Rebuilds complete frames from the USB chunks read off the frame endpoint."""

    def __init__(self):
        self._buf = bytearray()
        self.dropped = 0

    def feed(self, chunk: bytes) -> list[bytes]:
        out = []
        if chunk[:4] == MAGIC:          # a chunk starting with the magic begins a new frame
            if self._buf:
                self.dropped += 1
            self._buf = bytearray()
        self._buf += chunk
        while True:
            if len(self._buf) < HEADER_LEN:
                break
            if self._buf[:4] != MAGIC:  # lost sync: look for the next magic
                i = self._buf.find(MAGIC, 1)
                if i < 0:
                    self._buf = bytearray()
                    break
                del self._buf[:i]
                self.dropped += 1
                continue
            frame_size = struct.unpack_from("<I", self._buf, 8)[0]
            if frame_size > MAX_FRAME:
                del self._buf[:4]
                continue
            total = HEADER_LEN + frame_size
            if len(self._buf) < total:
                break
            out.append(bytes(self._buf[:total]))
            del self._buf[:total]
        return out


# --------------------------------------------------------------------------
# Radiometry
# --------------------------------------------------------------------------
@dataclass
class Radiometry:
    """Converts sensor counts to temperature with the Planck-type sensor model.

    The constants are the generic FLIR One values published by the open-source
    flirone-v4l2 driver. They are NOT this camera's factory calibration, so
    absolute temperature can be off by a few degC. Temperature *differences*
    within one image and changes over time are much more trustworthy, and
    those are what thermography uses.

    emissivity      0..1, how well the surface radiates compared with an ideal
                    black body (matte paint/tape ~0.95, bare metal ~0.1).
    reflected_c     temperature (degC) of the surroundings mirrored by the surface.
    """
    emissivity: float = 0.95
    reflected_c: float = 20.0
    planck_r1: float = 16528.178
    planck_r2: float = 0.012258549
    planck_b: float = 1427.5
    planck_f: float = 1.0
    planck_o: float = -1307.0
    raw_scale: float = 4.0   # wire counts are 1/4 of the counts the Planck constants expect

    def _raw_of_temp_c(self, t_c):
        return self.planck_r1 / (self.planck_r2 * (np.exp(self.planck_b / (np.asarray(t_c, float) + 273.15))
                                                   - self.planck_f)) - self.planck_o

    def to_celsius(self, raw) -> np.ndarray:
        r = np.asarray(raw, dtype=np.float64) * self.raw_scale
        refl = self._raw_of_temp_c(self.reflected_c)
        obj = (r - (1.0 - self.emissivity) * refl) / self.emissivity
        denom = self.planck_r2 * (obj + self.planck_o)
        with np.errstate(divide="ignore", invalid="ignore"):
            t = self.planck_b / np.log(self.planck_r1 / denom + self.planck_f) - 273.15
        return np.where(denom > 0, t, np.nan)

    def to_raw(self, t_c) -> np.ndarray:
        """Inverse of to_celsius (used by the simulator and the tests)."""
        obj = self._raw_of_temp_c(t_c)
        refl = self._raw_of_temp_c(self.reflected_c)
        r = self.emissivity * obj + (1.0 - self.emissivity) * refl
        return np.clip(np.round(r / self.raw_scale), 0, 65535).astype(np.uint16)


def build_wire_frame(raw: np.ndarray, jpeg: bytes = b"", status: dict | None = None) -> bytes:
    """Encode an image into the camera's wire format (simulator / tests)."""
    h, w = raw.shape
    layout = f"{w}x{h}"
    if layout not in LAYOUTS:
        raise FrameError(f"No layout for {layout}")
    _w, _h, row_words, runs = LAYOUTS[layout]
    rows = np.zeros((h, row_words), dtype="<u2")
    x = 0
    for a, b in runs:
        rows[:, a:b] = raw[:, x:x + (b - a)]
        x += b - a
    thermal = rows.tobytes()
    st = json.dumps(status or {}).encode()
    frame_size = len(thermal) + len(jpeg) + len(st)
    header = MAGIC + struct.pack("<I", 0) + struct.pack("<4I", frame_size, len(thermal), len(jpeg), len(st)) \
        + struct.pack("<I", 0)
    return header + thermal + jpeg + st
