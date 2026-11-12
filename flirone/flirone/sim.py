"""Simulated camera: lets the app be tried without hardware.

It produces frames in the real wire format and pushes them through the same
parser as the real camera. The scene is a warm plate with one spot that heats
up and cools down every 20 s (like a pulsed-thermography defect) on a ~22 degC
background, plus sensor-like noise.
"""
from __future__ import annotations

import threading
import time

import numpy as np

from .frame import FrameAssembler, Radiometry, build_wire_frame, parse_frame


class SimThread(threading.Thread):
    def __init__(self, on_frame, on_status=lambda s: None, size=(160, 120), fps=8.7):
        super().__init__(daemon=True)
        self.on_frame, self.on_status = on_frame, on_status
        self.w, self.h = size
        self.fps = fps
        self._halt = threading.Event()
        self.error = None

    def stop(self):
        self._halt.set()

    def run(self):
        rad = Radiometry()
        rng = np.random.default_rng(1)
        yy, xx = np.mgrid[0:self.h, 0:self.w]
        plate = ((abs(xx - self.w / 2) < self.w * 0.3) & (abs(yy - self.h / 2) < self.h * 0.3))
        spot = np.exp(-(((xx - self.w * 0.6) ** 2 + (yy - self.h * 0.45) ** 2) / (2 * (self.w * 0.04) ** 2)))
        asm = FrameAssembler()
        t0 = time.perf_counter()
        self.on_status("SIMULATION - no camera attached")
        n = 0
        while not self._halt.is_set():
            t = time.perf_counter() - t0
            phase = t % 20.0
            pulse = np.exp(-phase / 4.0) if phase > 1 else phase
            temp = 22.0 + 6.0 * plate + 5.0 * pulse * spot + rng.normal(0, 0.08, (self.h, self.w))
            wire = build_wire_frame(rad.to_raw(temp), status={"shutterState": "ON", "sim": True, "n": n})
            # deliver in USB-sized chunks to exercise the reassembly code
            for i in range(0, len(wire), 16384):
                for full in asm.feed(wire[i:i + 16384]):
                    self.on_frame(parse_frame(full, timestamp=time.perf_counter()))
            n += 1
            self._halt.wait(1.0 / self.fps)
