import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from flirone.frame import (FrameAssembler, Radiometry, build_wire_frame,  # noqa: E402
                           guess_layout, parse_frame)


def test_roundtrip_both_layouts():
    rng = np.random.default_rng(0)
    for w, h in ((160, 120), (80, 60)):
        raw = rng.integers(1000, 9000, (h, w)).astype(np.uint16)
        wire = build_wire_frame(raw, jpeg=b"\xff\xd8JPG\xff\xd9", status={"shutterState": "FFC"})
        fr = parse_frame(wire)
        assert fr.layout == f"{w}x{h}"
        assert np.array_equal(fr.raw, raw)
        assert fr.jpeg == b"\xff\xd8JPG\xff\xd9"
        assert fr.ffc


def test_reference_driver_indexing():
    """Same byte offsets as flirone-v4l2: pixel (x,y) at 2*(y*164+x)+32, +4 more for x>=80."""
    raw = (np.arange(160 * 120).reshape(120, 160) % 60000).astype(np.uint16)
    wire = build_wire_frame(raw)
    for x, y in ((0, 0), (79, 5), (80, 5), (159, 119)):
        off = 2 * (y * 164 + x) + 32 + (4 if x >= 80 else 0)
        assert wire[off] + 256 * wire[off + 1] == raw[y, x]


def test_assembler_chunks_and_resync():
    raw = np.full((60, 80), 3500, np.uint16)
    wire = build_wire_frame(raw)
    asm = FrameAssembler()
    got = []
    # garbage, a full frame, a truncated frame, then full frames (each USB transfer starts a frame)
    for part in (b"junkjunk" + wire, wire[:100], wire, wire):
        for i in range(0, len(part), 777):
            got += asm.feed(part[i:i + 777])
    assert len(got) == 3 and all(g == wire for g in got)
    assert asm.dropped >= 1


def test_radiometry_inverse_and_plausible():
    rad = Radiometry(emissivity=0.9, reflected_c=22)
    t = np.array([0.0, 20.0, 37.0, 100.0])
    back = rad.to_celsius(rad.to_raw(t))
    assert np.allclose(back, t, atol=0.05)
    assert 15 < Radiometry().to_celsius(3300) < 45     # room-temperature-ish counts


def test_guess_layout():
    assert guess_layout(39360) == "160x120"
    assert guess_layout(9840) == "80x60"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
